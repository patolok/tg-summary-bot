import requests
import os
import sqlite3
import pytz
import time
import datetime
import schedule
import json
from collections import Counter

API_BASE = "https://edu-api.21-school.ru/services/21-school/api/v1"
AUTH_URL = "https://auth.sberclass.ru/auth/realms/EduPowerKeycloak/protocol/openid-connect/token"
CAMPUS_ID = "5a23bec9-f989-485d-935b-3f0dc61c4812"
CLUSTER_IDS = ["36859", "36860", "36861", "36862"]
DB_NAME = "campus_attendance.db"
REPORT_FILE = "logins.txt"

# Учетные данные todo: вынести в файл конфигурации
USERNAME = "login"
PASSWORD = "pass"

# Глобальные переменные для хранения токенов
access_token = None
refresh_token = None
token_expiry = None


def get_new_tokens(username=USERNAME, password=PASSWORD, use_refresh=False):
    """
    Получение новых токенов через логин/пароль или через refresh token
    """
    global access_token, refresh_token, token_expiry

    headers = {'Content-Type': 'application/x-www-form-urlencoded'}

    if use_refresh and refresh_token:
        data = {
            'client_id': 's21-open-api',
            'grant_type': 'refresh_token',
            'refresh_token': refresh_token
        }
    else:
        data = {
            'client_id': 's21-open-api',
            'grant_type': 'password',
            'username': username,
            'password': password
        }

    try:
        response = requests.post(AUTH_URL, headers=headers, data=data)
        response.raise_for_status()  # Вызовет исключение при ошибке HTTP

        token_data = response.json()
        access_token = token_data.get('access_token')
        refresh_token = token_data.get('refresh_token')

        # Вычисляем время истечения токена, обычно expires_in в секундах
        # Берем немного меньшее значение для перестраховки
        expires_in = token_data.get('expires_in', 3600)
        token_expiry = datetime.datetime.now() + datetime.timedelta(seconds=expires_in * 0.9)

        print(f"Получены новые токены, действительны до {token_expiry}")
        return True

    except requests.exceptions.HTTPError as e:
        print(f"Ошибка получения токенов: {e}")
        if use_refresh:
            print("Попытка получения новых токенов через логин/пароль...")
            return get_new_tokens(username, password, use_refresh=False)
        return False
    except Exception as e:
        print(f"Непредвиденная ошибка при получении токенов: {e}")
        return False


def ensure_valid_token():
    """
    Проверяет и обновляет токен при необходимости
    """
    global access_token, token_expiry

    # Если токена нет или срок его действия истек, получаем новый
    if not access_token or token_expiry is None or datetime.datetime.now() >= token_expiry:
        if refresh_token:
            # Сначала пробуем через refresh token
            if not get_new_tokens(use_refresh=True):
                # Если не удалось обновить через refresh token, используем логин/пароль
                return get_new_tokens()
            return True
        else:
            # Если нет refresh токена, сразу используем логин/пароль
            return get_new_tokens()
    return True


def is_token_valid(response):
    """
    Проверяет ответ API на признаки недействительного токена
    """
    # Проверка кода ответа
    if response.status_code == 400:
        # Проверяем текст ответа
        try:
            if "Invalid token" in response.text:
                return False
        except:
            pass

    # Также проверяем коды авторизации
    if response.status_code in [401, 403]:
        return False

    # Проверка содержимого ответа на наличие сообщений об ошибке токена
    try:
        body = response.json()
        error_msg = str(body.get('error', '')).lower()
        if 'token' in error_msg and ('expired' in error_msg or 'invalid' in error_msg):
            return False
    except:
        pass

    return True


def get_cluster_logins(cluster_id):
    """Получение логинов пользователей в кластере"""
    if not ensure_valid_token():
        print(f"Не удалось получить действующий токен для кластера {cluster_id}")
        return []

    url = f"{API_BASE}/clusters/{cluster_id}/map"
    headers = {"Authorization": f"Bearer {access_token}"}

    resp = requests.get(url, headers=headers, timeout=10)

    # Проверяем действительность токена
    if not is_token_valid(resp):
        print("Токен недействителен, обновляем...")
        if ensure_valid_token():
            # Повторяем запрос с новым токеном
            headers = {"Authorization": f"Bearer {access_token}"}
            resp = requests.get(url, headers=headers)
        else:
            print("Не удалось обновить токен")
            return []

    # Проверяем ответ
    if resp.status_code != 200:
        print(f"Ошибка при получении карты кластера {cluster_id}: {resp.status_code} {resp.text}")
        return []

    try:
        cluster_map = resp.json()
    except Exception as e:
        print(f"Ошибка при разборе ответа: {e}")
        return []

    if not isinstance(cluster_map, dict) or 'clusterMap' not in cluster_map:
        print("Некорректный формат ответа API")
        return []

    logins = [place['login'] for place in cluster_map['clusterMap'] if place.get('login')]
    return logins


def init_database():
    """Инициализация базы данных, если ее еще нет"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY,
            check_time TIMESTAMP,
            login TEXT,
            UNIQUE(check_time, login)
        )
    ''')
    conn.commit()
    conn.close()


def save_to_db(logins):
    """Сохранение списка логинов в базу данных"""
    if not logins:
        return

    now = datetime.datetime.now()
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    for login in logins:
        try:
            cursor.execute(
                "INSERT INTO attendance (check_time, login) VALUES (?, ?)",
                (now, login)
            )
        except sqlite3.IntegrityError:
            # Пропускаем дубликаты
            pass

    conn.commit()
    conn.close()
    print(f"{now}: Сохранено {len(logins)} логинов в базу данных")


def check_attendance():
    """Проверка присутствия в кампусе"""
    all_logins = []
    for cluster_id in CLUSTER_IDS:
        logins = get_cluster_logins(cluster_id)
        all_logins.extend(logins)

    now = datetime.datetime.now()
    print(f"{now}: Обнаружено {len(all_logins)} залогиненных пользователей")

    save_to_db(all_logins)


def load_valid_student_logins(filename="students.txt"):
    """Загружает список допустимых логинов студентов из файла"""
    if not os.path.exists(filename):
        print(f"Файл {filename} не найден.")
        return set()
    with open(filename, 'r', encoding='utf-8') as f:
        return set(line.strip() for line in f if line.strip())


def get_weekly_unique_logins():
    """Возвращает количество уникальных логинов за последние 7 дней"""
    valid_logins = load_valid_student_logins()
    today = datetime.datetime.now().date()
    start_time = datetime.datetime.combine(today - datetime.timedelta(days=6), datetime.time(7, 0))
    end_time = datetime.datetime.combine(today, datetime.time(20, 45))

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT DISTINCT login FROM attendance WHERE check_time BETWEEN ? AND ?",
        (start_time, end_time)
    )
    records = cursor.fetchall()
    conn.close()

     # Фильтрация по допустимым логинам
    filtered_logins = {login for (login,) in records if login in valid_logins}
    return len(filtered_logins)

def get_days_until_deadline():
    """Возвращает количество дней до следующего дедлайна"""
    tz = pytz.timezone('Europe/Moscow')
    now = datetime.datetime.now(tz)
    target_date = datetime.datetime(2025, 9, 21, tzinfo=tz)
    return (target_date.date() - now.date()).days - 1

def generate_daily_report():
    """Генерация ежедневного отчета"""
    today = datetime.datetime.now().date()
    start_time = datetime.datetime.combine(today, datetime.time(7, 0))
    end_time = datetime.datetime.combine(today, datetime.time(20, 40))
    
    valid_logins = load_valid_student_logins()  # <-- Загружаем валидные логины

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # Получаем все записи за сегодня между 7:00 и 20:45
    cursor.execute(
        "SELECT check_time, login FROM attendance WHERE check_time BETWEEN ? AND ?",
        (start_time, end_time)
    )
    records = cursor.fetchall()

    # Подсчитываем уникальные логины
    unique_logins = set()
    time_login_counts = {}

    for check_time_str, login in records:
        if login not in valid_logins:
            continue  # Пропускаем, если логин не из списка студентов
    
        unique_logins.add(login)

        # Округляем время до получаса для подсчета пиковой нагрузки
        check_time = datetime.datetime.fromisoformat(check_time_str)
        time_key = check_time.replace(second=0, microsecond=0)

        if time_key not in time_login_counts:
            time_login_counts[time_key] = set()
        time_login_counts[time_key].add(login)

    # Находим время с максимальным количеством логинов
    peak_time = None
    peak_count = 0

    for time_key, logins_set in time_login_counts.items():
        count = len(logins_set)
        if count > peak_count:
            peak_count = count
            peak_time = time_key

    conn.close()

    # Формируем отчет
    lines = ["🏫 **Посещаемость и срок сдачи:**"]

    lines.append(f"- Дней до ближайшего ддл: {get_days_until_deadline()}")

    # Если воскресенье — добавим строку о логинах за неделю
    if datetime.datetime.now().weekday() == 6:  # 6 = Sunday
        weekly_logins = get_weekly_unique_logins()
        lines.append(f"- Уник. логинов за неделю: {weekly_logins}")

    # Добавляем данные за текущий день
    lines.append(f"- Уник. логинов за день: {len(unique_logins)}")
    if peak_time:
        lines.append(f"- Час пик: {peak_time.strftime('%H:%M')}")

    # Записываем отчет в файл
    with open(REPORT_FILE, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

    print(f"Отчет за {today} сохранен в {REPORT_FILE}")


def is_working_hour():
    """Проверяет, находимся ли мы в рабочее время (7:00 - 20:45)"""
    now = datetime.datetime.now().time()
    start = datetime.time(7, 0)
    end = datetime.time(20, 45)
    return start <= now <= end


def main():
    """Основная функция для запуска как сервис"""
    print("Запуск сервиса мониторинга кампуса...")
    init_database()

    # Получаем токены при запуске
    if not ensure_valid_token():
        print("Не удалось получить токены при запуске. Проверьте учетные данные.")
        return

    # Планируем проверки каждые 15 минут в рабочее время
    for hour in range(7, 21):
        for minute in [0, 15, 30, 45]:
            if hour == 20 and minute > 45:
                continue
            schedule.every().day.at(f"{hour:02d}:{minute:02d}").do(check_attendance)

    # Планируем генерацию отчета в 20:50
    schedule.every().day.at("20:50").do(generate_daily_report)

    # Проверяем сразу при запуске, если время рабочее
    if is_working_hour():
        check_attendance()

    # Основной цикл
    while True:
        schedule.run_pending()
        time.sleep(60)  # Проверяем расписание каждую минуту


if __name__ == "__main__":
    main()
