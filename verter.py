import os
import sys
import sqlite3
import logging
import asyncio
import pytz
import re
from datetime import datetime, timedelta, time as dt_time
from pathlib import Path

from telegram import Update, Chat, Message
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
)

from google import genai

# --- Константы ---
CONFIG_PATH = 'config.txt'
DB_PATH = 'messages.db'
MESSAGES_DIR = 'messages'

# --- Логгирование ---
logging.basicConfig(
    format='%(asctime)s %(levelname)s: %(message)s',
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)]
)
# Отключаем подробный лог сторонних библиотек
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("aiogram").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- Чтение конфига ---
def read_config(path):
    config = {}
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                key, val = line.split('=', 1)
                config[key.strip()] = val.strip()
    # Преобразуем нужные поля
    config['TARGET_CHAT_ID'] = int(config['TARGET_CHAT_ID'])
    # SUMMARY_TOPIC_ID теперь опционален
    if 'SUMMARY_TOPIC_ID' in config and config['SUMMARY_TOPIC_ID'].strip():
        config['SUMMARY_TOPIC_ID'] = int(config['SUMMARY_TOPIC_ID'])
    else:
        config['SUMMARY_TOPIC_ID'] = None
    config['MAX_SUMMARY_SIZE'] = int(config['MAX_SUMMARY_SIZE'])
    config['IGNORED_TOPIC_IDS'] = [
        int(x) for x in config.get('IGNORED_TOPIC_IDS', '').split(',') if x.strip().isdigit()
    ]
    # Новый ключ для Gemini
    config['GEMINI_API_KEY'] = config.get('GEMINI_API_KEY', '')
    return config

# --- Инициализация базы ---
def init_db(db_path):
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            message_id INTEGER PRIMARY KEY,
            username TEXT,
            message_text TEXT,
            timestamp TEXT,
            chat_id INTEGER,
            thread_id INTEGER
        )
    ''')
    conn.commit()
    conn.close()

# --- Сохранение сообщения ---
def save_message(db_path, message_id, username, message_text, timestamp, chat_id, thread_id):
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute('''
        INSERT OR IGNORE INTO messages (message_id, username, message_text, timestamp, chat_id, thread_id)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (message_id, username, message_text, timestamp, chat_id, thread_id))
    conn.commit()
    conn.close()

# --- Получение сообщений за период ---
def fetch_messages_for_period(db_path, chat_id, from_dt, to_dt, ignored_thread_ids):
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    query = '''
        SELECT message_id, username, message_text, timestamp, thread_id
        FROM messages
        WHERE chat_id = ?
          AND timestamp >= ?
          AND timestamp < ?
    '''
    params = [chat_id, from_dt.isoformat(), to_dt.isoformat()]
    if ignored_thread_ids:
        query += ' AND (thread_id IS NULL OR thread_id NOT IN (%s))' % (
            ','.join(['?']*len(ignored_thread_ids))
        )
        params.extend(ignored_thread_ids)
    query += ' ORDER BY timestamp ASC'
    c.execute(query, params)
    rows = c.fetchall()
    conn.close()
    return rows

# --- Экспорт сообщений в файл, генерация summary через Gemini ---
def export_messages(config):
    tz = pytz.timezone('Europe/Moscow')
    now = datetime.now(tz)
    export_time = datetime.combine(now.date(), dt_time.fromisoformat(config['TIME_EXPORT']), tz)
    if now < export_time:
        export_time -= timedelta(days=1)
    from_dt = export_time
    to_dt = export_time + timedelta(days=1)
    date_str = to_dt.strftime('%d.%m.%y')  # dd.mm.yy
    export_dir = Path(MESSAGES_DIR) / date_str
    export_dir.mkdir(parents=True, exist_ok=True)

    messages = fetch_messages_for_period(
        DB_PATH,
        config['TARGET_CHAT_ID'],
        from_dt,
        to_dt,
        config['IGNORED_TOPIC_IDS']
    )
    logger.info(f"Exporting {len(messages)} messages for {date_str}")

    # Формируем строки
    lines = [f"{message_id} | {username}: {text}" for message_id, username, text, _, _ in messages]
    fname = export_dir / "messages.txt"
    with open(fname, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    logger.info(f"Exported all messages to {fname}")

    # Проверка на пустой день (только пустые строки/пробелы)
    if not any(line.strip() for line in lines):
        # Город спит...
        tz = pytz.timezone('Europe/Moscow')
        now = datetime.now(tz)
        start_date = datetime(2025, 4, 23, tzinfo=tz)
        day_number = (now.date() - start_date.date()).days
        summary_path = export_dir / 'summary.txt'
        msg = f"✨{day_number}-й день основы\n🌙 Город спит..."
        with open(summary_path, 'w', encoding='utf-8') as f:
            f.write(msg)
        logger.info("No messages for the day. Posted 'Город спит...'")
        return

    # Генерируем summary через Gemini
    try:
        summary = generate_summary_via_gemini(config, fname)
        summary_path = export_dir / 'summary.txt'
        with open(summary_path, 'w', encoding='utf-8') as f:
            f.write(summary.rstrip())
        logger.info(f"Summary saved to {summary_path}")
    except Exception as e:
        logger.error(f"Failed to generate summary via Gemini: {e}")

# --- Gemini summary helper ---
def generate_summary_via_gemini(config, messages_path):
    # Прочитать promt.txt
    promt_path = Path(__file__).parent / 'promt.txt'
    with open(promt_path, encoding='utf-8') as f:
        prompt = f.read().strip()
    # Прочитать сообщения
    with open(messages_path, encoding='utf-8') as f:
        messages = f.read().strip()
    # Объединить промт и сообщения
    full_prompt = f"{prompt}\n{messages}"
    # Отправить в Gemini
    api_key = config['GEMINI_API_KEY']
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=full_prompt
    )
    return response.text

def escape_markdown_v2(text):
    # Найти все телеграм-ссылки
    link_pattern = re.compile(r'\[🔗\]\(https://t\.me/c/\d+/[^)]+\)')
    # Найти заголовки в формате **Текст**
    header_pattern = re.compile(r'\*\*([^\*]+)\*\*')

    result = []
    last_idx = 0

    # Обрабатываем текст, учитывая ссылки и заголовки
    for match in re.finditer(r'(\[🔗\]\(https://t\.me/c/\d+/[^)]+\))|(\*\*[^\*]+\*\*)', text):
        # Экранируем текст до текущего совпадения (ссылки или заголовка)
        before = text[last_idx:match.start()]
        special_chars = r'_*[]()~>#+\-=|{}.!'
        before = re.sub(f'([{re.escape(special_chars)}])', r'\\\1', before)
        result.append(before)

        # Если это ссылка, добавляем как есть
        if match.group(1):
            result.append(match.group(1))
        # Если это заголовок, преобразуем в *Текст* для жирного начертания
        elif match.group(2):
            header_text = match.group(2)[2:-2]  # Удаляем ** с начала и конца
            # Экранируем специальные символы внутри заголовка
            header_text = re.sub(f'([{re.escape(special_chars)}])', r'\\\1', header_text)
            result.append(f'*{header_text}*')

        last_idx = match.end()

    # Экранируем остаток текста после последнего совпадения
    special_chars = r'_*[]()~>#+\-=|{}.!'
    after = text[last_idx:]
    after = re.sub(f'([{re.escape(special_chars)}])', r'\\\1', after)
    result.append(after)

    return ''.join(result)

# --- Публикация summary ---
async def post_summary(config, application):
    tz = pytz.timezone('Europe/Moscow')
    now = datetime.now(tz)
    date_str = now.strftime('%d.%m.%y')  # dd.mm.yy
    export_dir = Path(MESSAGES_DIR) / date_str
    summary_path = export_dir / 'summary.txt'
    if not summary_path.exists():
        logger.warning(f"Looking for summary.txt at {summary_path}")
        logger.warning(f"No summary.txt found for {date_str}")
        return
    with open(summary_path, encoding='utf-8') as f:
        summary = f.read().strip()
    if len(summary) > config['MAX_SUMMARY_SIZE']:
        logger.warning(f"Summary exceeds MAX_SUMMARY_SIZE ({len(summary)} > {config['MAX_SUMMARY_SIZE']})")
        return
    start_date = datetime(2025, 4, 23, tzinfo=tz)
    day_number = (now.date() - start_date.date()).days
    # Если в summary уже содержится 'Город спит...', просто отправляем его
    if 'Город спит...' in summary:
        msg = summary
    else:
        msg = f"✨{day_number}-й день основы #Темы_дня\n{summary}"
    # --- ДОБАВЛЕНИЕ содержимого logins.txt ---
    logins_path = Path('logins.txt')
    if logins_path.exists():
        try:
            with open(logins_path, encoding='utf-8') as f:
                logins_content = f.read().strip()
            if logins_content:
                msg += f"\n\n{logins_content}"
        except Exception as e:
            logger.error(f"Failed to read logins.txt: {e}")
    else:
        logger.warning(f"logins.txt not found at {logins_path.resolve()}")
    # --- Экранирование спецсимволов для MarkdownV2 ---
    msg = escape_markdown_v2(msg)
    send_args = dict(
        chat_id=config['TARGET_CHAT_ID'],
        text=msg,
        parse_mode="MarkdownV2"
    )
    if config.get('SUMMARY_TOPIC_ID') is not None:
        send_args['message_thread_id'] = config['SUMMARY_TOPIC_ID']
    try:
        await application.bot.send_message(**send_args)
        logger.info("Summary posted successfully.")
    except Exception as e:
        logger.error(f"Failed to post summary: {e}")

# --- Планировщик задач ---
async def scheduler(config, application):
    tz = pytz.timezone('Europe/Moscow')
    last_export_date = None
    last_post_date = None
    while True:
        now = datetime.now(tz)
        # Экспорт сообщений
        export_time = dt_time.fromisoformat(config['TIME_EXPORT'])
        naive_export_dt = datetime.combine(now.date(), export_time)  # naive datetime
        next_export = tz.localize(naive_export_dt)
        if now >= next_export:
            next_export += timedelta(days=1)
        # Публикация summary
        post_time = dt_time.fromisoformat(config['TIME_POST'])
        naive_post_dt = datetime.combine(now.date(), post_time)  # naive datetime
        next_post = tz.localize(naive_post_dt)
        if now >= next_post:
            next_post += timedelta(days=1)
        # Sleep до ближайшего события
        sleep_seconds = min((next_export - now).total_seconds(), (next_post - now).total_seconds())
        await asyncio.sleep(max(1, int(sleep_seconds)))
        now = datetime.now(tz)
        if abs((now - next_export).total_seconds()) < 60 and last_export_date != now.date():
            export_messages(config)
            last_export_date = now.date()
        if abs((now - next_post).total_seconds()) < 60 and last_post_date != now.date():
            await post_summary(config, application)
            last_post_date = now.date()

# --- Обработчик сообщений ---
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = context.bot_data['config']
    msg: Message = update.effective_message
    chat: Chat = update.effective_chat

    # Только целевая группа
    if chat.id != config['TARGET_CHAT_ID']:
        return
    # Игнорировать репосты из каналов
    if getattr(msg, 'forward_from_chat', None) is not None and getattr(msg.forward_from_chat, 'type', None) == 'channel':        return
    # Игнорировать личные сообщения и другие группы
    if chat.type not in ['group', 'supergroup']:
        return
    # Игнорировать команды
    if msg.text and msg.text.startswith('/'):
        return
    # Игнорировать сообщения из игнорируемых топиков
    thread_id = getattr(msg, 'message_thread_id', None)
    if thread_id is not None and thread_id in config['IGNORED_TOPIC_IDS']:
        return

    username = msg.from_user.username or msg.from_user.first_name or "Unknown"
    message_text = msg.text or msg.caption or ''
    if not message_text.strip():
        return
    if len(message_text) > 850:
        return
    timestamp = datetime.fromtimestamp(msg.date.timestamp(), pytz.UTC).astimezone(pytz.timezone('Europe/Moscow')).isoformat()
    save_message(
        DB_PATH,
        msg.message_id,
        username,
        message_text,
        timestamp,
        chat.id,
        thread_id
    )
    logger.info(f"Saved message from {username} (id={msg.message_id})")

# --- Main ---
def main():
    # Проверка путей
    Path(MESSAGES_DIR).mkdir(exist_ok=True)
    config = read_config(CONFIG_PATH)
    init_db(DB_PATH)

    # Запуск бота
    application = ApplicationBuilder().token(config['TOKEN']).build()
    application.bot_data['config'] = config

    # Обработка всех текстовых сообщений
    application.add_handler(MessageHandler(filters.ALL, on_message))

    # Планировщик
    loop = asyncio.get_event_loop()
    loop.create_task(scheduler(config, application))

    logger.info("Bot started.")
    application.run_polling()

if __name__ == '__main__':
    main()
