import os
import sys
import sqlite3
import logging
import asyncio
import pytz
from datetime import datetime, timedelta, time as dt_time
from pathlib import Path

from telegram import Update, Chat, Message
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
)

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
    config['SUMMARY_TOPIC_ID'] = int(config['SUMMARY_TOPIC_ID'])
    config['MAX_FILE_SIZE'] = int(config['MAX_FILE_SIZE'])
    config['MAX_SUMMARY_SIZE'] = int(config['MAX_SUMMARY_SIZE'])
    config['IGNORED_TOPIC_IDS'] = [
        int(x) for x in config.get('IGNORED_TOPIC_IDS', '').split(',') if x.strip().isdigit()
    ]
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
        SELECT username, message_text, timestamp, thread_id
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

# --- Экспорт сообщений в файлы ---
def export_messages(config):
    tz = pytz.timezone('Europe/Moscow')
    now = datetime.now(tz)
    export_time = datetime.combine(now.date(), dt_time.fromisoformat(config['TIME_EXPORT']), tz)
    if now < export_time:
        export_time -= timedelta(days=1)
    from_dt = export_time
    to_dt = export_time + timedelta(days=1)
    date_str = to_dt.strftime('%d.%m.%Y')
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
    lines = [f"{username}: {text}" for username, text, _, _ in messages]
    max_size = config['MAX_FILE_SIZE']
    file_idx = 1
    curr_lines = []
    curr_size = 0
    files = []
    for line in lines:
        line_size = len(line) + 1  # +1 for newline
        if curr_size + line_size > max_size and curr_lines:
            fname = export_dir / f"messages_part{file_idx}.txt"
            with open(fname, 'w', encoding='utf-8') as f:
                f.write('\n'.join(curr_lines))
            files.append(fname)
            file_idx += 1
            curr_lines = []
            curr_size = 0
        curr_lines.append(line)
        curr_size += line_size
    if curr_lines:
        fname = export_dir / f"messages_part{file_idx}.txt"
        with open(fname, 'w', encoding='utf-8') as f:
            f.write('\n'.join(curr_lines))
        files.append(fname)
    logger.info(f"Exported to {len(files)} file(s) in {export_dir}")

# --- Публикация summary ---
async def post_summary(config, application):
    tz = pytz.timezone('Europe/Moscow')
    now = datetime.now(tz)
    date_str = now.strftime('%d.%m.%Y')
    export_dir = Path(MESSAGES_DIR) / date_str
    summary_path = export_dir / 'summary.txt'
    if not summary_path.exists():
        logger.warning(f"Looking for summary.txt at {summary_path}")
        logger.warning(f"No summary.txt found for {date_str}")
        return
    with open(summary_path, encoding='utf-8') as f:
        summary = f.read()
    if len(summary) > config['MAX_SUMMARY_SIZE']:
        logger.warning(f"Summary exceeds MAX_SUMMARY_SIZE ({len(summary)} > {config['MAX_SUMMARY_SIZE']})")
        return
    msg = f"Сегодня {now.strftime('%d.%m')} в теме обсуждалось:\n{summary}"
    try:
        await application.bot.send_message(
            chat_id=config['TARGET_CHAT_ID'],
            message_thread_id=config['SUMMARY_TOPIC_ID'],
            text=msg
        )
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
    if getattr(msg, 'forward_from_chat', None) is not None and getattr(msg.forward_from_chat, 'type', None) == 'channel':
        return
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