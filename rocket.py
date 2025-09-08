import requests
import json
import time
import re
from datetime import datetime
from typing import Dict, Set, List, Optional
import os
import asyncio
from telegram import Bot
import traceback

# === ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ===
known_message_ids: Dict[str, Set[str]] = {}  # {room_id: {message_ids}}

def escape_markdown_v2(text: str) -> str:
    # Все зарезервированные символы MarkdownV2 (включая точку и обратный слэш)
    specials_re = re.compile(r'([_\*\[\]\(\)~`>#+\-=|{}\.\!\\])')

    # Находит либо ссылку [text](url), либо парный *...*
    token_re = re.compile(r'(\[[^\]]+\]\([^\)]+\))|(\*([^*]+?)\*)', flags=re.DOTALL)

    def escape_all(s: str) -> str:
        return specials_re.sub(lambda m: '\\' + m.group(1), s)

    parts = []
    last = 0
    for m in token_re.finditer(text):
        # участок до токена — экранируем полностью
        before = text[last:m.start()]
        if before:
            parts.append(escape_all(before))
        # если это ссылка — оставляем как есть
        if m.group(1):
            parts.append(m.group(1))
        else:
            # это *...* — оставляем звездочки, но экранируем содержимое
            inner = m.group(3)
            parts.append('*' + escape_all(inner) + '*')
        last = m.end()
    # хвост после последнего совпадения
    tail = text[last:]
    if tail:
        parts.append(escape_all(tail))
    return ''.join(parts)

def load_config(config_path: str = "config.txt") -> Dict[str, str]:
    """Загружает конфигурацию из файла config.txt"""
    config = {}
    
    # Параметры по умолчанию
    default_config = {
        "ROCKET_URL": "",
        "ROCKET_USER_TOKEN": "",
        "ROCKET_USER_ID": "",
        "ROCKET_GROUP_IDS": "",
        "ROCKET_CHANNEL_IDS": "",
        "ROCKET_CHECK_INTERVAL": "1",
        "ROCKET_FILTER_USERS": "",  # Пустое значение = показывать всех
        "TOKEN": "",
        "TARGET_CHAT_ID": "",
        "POST_THREAD_ID": ""
    }
    
    # Пытаемся загрузить конфигурацию из файла
    if os.path.exists(config_path):
        print(f"Загрузка конфигурации из {config_path}...")
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                
            for line in lines:
                line = line.strip()
                # Пропускаем пустые строки и комментарии
                if not line or line.startswith('#'):
                    continue
                    
                # Ищем параметры формата KEY=VALUE
                if '=' in line:
                    key, value = line.split('=', 1)
                    key = key.strip()
                    value = value.strip()
                    
                    # Проверяем, является ли это нашим параметром
                    if key in default_config:
                        config[key] = value
                        
        except Exception as e:
            print(f"Ошибка чтения конфигурации: {e}")
    else:
        print(f"Файл конфигурации {config_path} не найден. Создаю шаблон...")
        create_config_template(config_path, default_config)
        print(f"Шаблон создан. Пожалуйста, заполните {config_path} и запустите скрипт снова.")
        exit(1)
    
    # Применяем значения по умолчанию для отсутствующих параметров
    for key, default_value in default_config.items():
        if key not in config:
            config[key] = default_value
    
    # Валидация обязательных параметров
    required_params = ["ROCKET_URL", "ROCKET_USER_TOKEN", "ROCKET_USER_ID", "TOKEN", "TARGET_CHAT_ID", "POST_THREAD_ID"]
    missing_params = [p for p in required_params if not config.get(p)]
    
    if missing_params:
        print(f"Ошибка: Отсутствуют обязательные параметры в конфигурации: {', '.join(missing_params)}")
        print(f"Пожалуйста, заполните их в файле {config_path}")
        exit(1)
    
    return config

def create_config_template(config_path: str, default_config: Dict[str, str]):
    """Создает шаблон конфигурационного файла"""
    template = """# Конфигурация для RocketChat Monitor
# Заполните параметры ниже

# URL вашего сервера RocketChat (например: https://chat.example.com)
ROCKET_URL=

# Токен аутентификации
ROCKET_USER_TOKEN=

# ID пользователя
ROCKET_USER_ID=

# ID групп для мониторинга (через запятую, например: groupId1,groupId2)
ROCKET_GROUP_IDS=

# ID каналов для мониторинга (через запятую, например: channelId1,channelId2)
ROCKET_CHANNEL_IDS=

# Интервал проверки в минутах
ROCKET_CHECK_INTERVAL=1

# Имена пользователей для фильтрации (через запятую, например: user1,user2)
# Оставьте пустым, чтобы показывать сообщения от всех пользователей
ROCKET_FILTER_USERS=

# Telegram настройки
TOKEN=
TARGET_CHAT_ID=  # ID группы (с минусом!)
POST_THREAD_ID=  # ID конкретного треда

# Другие параметры вашей системы могут быть добавлены ниже
"""
    
    try:
        with open(config_path, 'w', encoding='utf-8') as f:
            f.write(template)
    except Exception as e:
        print(f"Ошибка создания шаблона конфигурации: {e}")

def parse_list_param(param: str) -> List[str]:
    """Преобразует строку с разделителями-запятыми в список"""
    if not param:
        return []
    return [item.strip() for item in param.split(',') if item.strip()]

def get_messages_from_room(room_id: str, room_type: str, config: Dict[str, str]) -> List[Dict]:
    """Получает сообщения из комнаты (группы или канала)"""
    
    # Определяем URL в зависимости от типа комнаты
    if room_type == "group":
        endpoint = "/api/v1/groups.messages"
    elif room_type == "channel":
        endpoint = "/api/v1/channels.messages"
    else:
        print(f"Неизвестный тип комнаты: {room_type}")
        return []
    
    url = f"{config['ROCKET_URL']}{endpoint}"
    headers = {
        "X-Auth-Token": config['ROCKET_USER_TOKEN'],
        "X-User-Id": config['ROCKET_USER_ID'],
        "Content-Type": "application/json"
    }
    params = {
        "roomId": room_id
    }
    
    try:
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        
        data = response.json()
        if data.get("success", False):
            return data.get("messages", [])
        else:
            print(f"Ошибка API для {room_type} {room_id}: {data}")
            return []
            
    except requests.exceptions.RequestException as e:
        print(f"Ошибка запроса для {room_type} {room_id}: {e}")
        return []

def format_message(message: Dict, room_type: str, room_id: str) -> str:
    """Форматирует сообщение для вывода"""
    msg_text = message.get("msg", "")
    timestamp = message.get("ts", "")
    username = message.get("u", {}).get("username", "unknown")
    
    # Форматируем время
    try:
        dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        formatted_time = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except:
        formatted_time = timestamp
    
    room_prefix = "Группа" if room_type == "group" else "Канал"
    
    return f"\n[{formatted_time}] {room_prefix} {room_id}\n{username}:\n{msg_text}\n"

def is_thread_reply(message: Dict) -> bool:
    """Проверяет, является ли сообщение ответом в треде"""
    return "tmid" in message

def should_show_message(message: Dict, filter_users: List[str]) -> bool:
    """Проверяет, должно ли сообщение быть показано на основе фильтра пользователей"""
    # Если фильтр пустой, показываем все сообщения
    if not filter_users:
        return True
    
    # Получаем имя пользователя из сообщения
    username = message.get("u", {}).get("username", "")
    
    # Проверяем, входит ли пользователь в список фильтра
    return username in filter_users

def initialize_known_messages(config: Dict[str, str]):
    """Инициализирует известные сообщения при запуске"""
    print("=" * 50)
    print("ИНИЦИАЛИЗАЦИЯ МОНИТОРИНГА")
    print("=" * 50)
    
    group_ids = parse_list_param(config.get('ROCKET_GROUP_IDS', ''))
    channel_ids = parse_list_param(config.get('ROCKET_CHANNEL_IDS', ''))
    filter_users = parse_list_param(config.get('ROCKET_FILTER_USERS', ''))
    
    print(f"Групп для мониторинга: {len(group_ids)}")
    print(f"Каналов для мониторинга: {len(channel_ids)}")
    print(f"Интервал проверки: {config.get('ROCKET_CHECK_INTERVAL', '1')} минут")
    
    if filter_users:
        print(f"Фильтр пользователей: {', '.join(filter_users)}")
    else:
        print("Фильтр пользователей: отключен (показываются все)")
    
    print("-" * 50)
    
    # Инициализация групп
    for room_id in group_ids:
        print(f"Загрузка сообщений из группы {room_id}...")
        messages = get_messages_from_room(room_id, "group", config)
        if messages:
            message_ids = {msg["_id"] for msg in messages if "_id" in msg}
            known_message_ids[f"group_{room_id}"] = message_ids
            print(f"  Загружено {len(message_ids)} сообщений")
        else:
            known_message_ids[f"group_{room_id}"] = set()
            print(f"  Сообщения не найдены или ошибка доступа")
    
    # Инициализация каналов
    for room_id in channel_ids:
        print(f"Загрузка сообщений из канала {room_id}...")
        messages = get_messages_from_room(room_id, "channel", config)
        if messages:
            message_ids = {msg["_id"] for msg in messages if "_id" in msg}
            known_message_ids[f"channel_{room_id}"] = message_ids
            print(f"  Загружено {len(message_ids)} сообщений")
        else:
            known_message_ids[f"channel_{room_id}"] = set()
            print(f"  Сообщения не найдены или ошибка доступа")
    
    print("=" * 50)
    print("Инициализация завершена. Мониторинг новых сообщений...")
    print("=" * 50 + "\n")

async def check_for_new_messages(config: Dict[str, str], bot: Bot):
    """Проверяет новые сообщения во всех отслеживаемых комнатах"""
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{current_time}] Проверка новых сообщений...")
    
    group_ids = parse_list_param(config.get('ROCKET_GROUP_IDS', ''))
    channel_ids = parse_list_param(config.get('ROCKET_CHANNEL_IDS', ''))
    filter_users = parse_list_param(config.get('ROCKET_FILTER_USERS', ''))
    
    total_new_messages = 0
    rooms_to_check = []
    
    # Добавляем группы в список проверки
    for room_id in group_ids:
        rooms_to_check.append(("group", room_id))
    
    # Добавляем каналы в список проверки
    for room_id in channel_ids:
        rooms_to_check.append(("channel", room_id))
    
    # Проверяем каждую комнату
    for room_type, room_id in rooms_to_check:
        messages = get_messages_from_room(room_id, room_type, config)
        
        if not messages:
            continue
        
        storage_key = f"{room_type}_{room_id}"
        
        # Инициализируем, если комната новая
        if storage_key not in known_message_ids:
            known_message_ids[storage_key] = set()
        
        # Получаем ID всех текущих сообщений
        current_message_ids = {msg["_id"] for msg in messages if "_id" in msg}
        
        # Находим новые сообщения
        new_message_ids = current_message_ids - known_message_ids[storage_key]
        
        if new_message_ids:
            # Находим новые сообщения
            new_messages = [msg for msg in messages if msg.get("_id") in new_message_ids]
            
            # Фильтруем:
            # 1. Исключаем ответы в тредах
            # 2. Применяем фильтр по пользователям
            filtered_messages = [
                msg for msg in new_messages 
                if not is_thread_reply(msg) and should_show_message(msg, filter_users)
            ]
            
            if filtered_messages:
                # Сортируем по времени создания
                filtered_messages.sort(key=lambda x: x.get("ts", ""))
                
                for message in filtered_messages:
                    print(format_message(message, room_type, room_id))
                    
                    username = message.get("u", {}).get("username", "unknown")
                    msg_text = message.get("msg", "")
                    escaped_user = escape_markdown_v2(username)
                    escaped_msg = escape_markdown_v2(msg_text)
                    
                    telegram_message = f"🚀 *{escaped_user}:*\n{escaped_msg}\n"
                    
                    await bot.send_message(
                        chat_id=int(config['TARGET_CHAT_ID']),
                        text=telegram_message,
                        parse_mode="MarkdownV2",
                        message_thread_id=int(config['POST_THREAD_ID'])
                    )
                    
                    total_new_messages += 1
            
            # Обновляем список известных сообщений (включая все новые)
            known_message_ids[storage_key] = current_message_ids
    
    if total_new_messages == 0:
        print("  Новых сообщений нет")
    else:
        print(f"  Всего новых сообщений: {total_new_messages}")

async def main():
    """Главная функция программы"""
    # Загружаем конфигурацию
    config = load_config()
    
    # Инициализируем известные сообщения
    initialize_known_messages(config)
    
    # Создаем Telegram бот
    bot = Bot(token=config['TOKEN'])
    
    # Получаем интервал проверки
    check_interval = int(config.get('ROCKET_CHECK_INTERVAL', '1'))
    
    try:
        while True:
            await check_for_new_messages(config, bot)
            print(f"Ожидание {check_interval} минут до следующей проверки...\n")
            await asyncio.sleep(check_interval * 60)
            
    except KeyboardInterrupt:
        print("\n\nМониторинг остановлен пользователем")
    except Exception as e:
        print(f"\nНеожиданная ошибка: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())

