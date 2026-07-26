import os
import json
import imaplib
import smtplib
import email
from email.header import decode_header
from email.mime.text import MIMEText
import re
import time
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Переменные окружения
EMAIL_USER = os.getenv("EMAIL_ACCOUNT")
EMAIL_PASS = os.getenv("EMAIL_PASSWORD")
FOLDER_ID = os.getenv("GDRIVE_FOLDER_ID")
SERVICE_KEY_JSON = os.getenv("GDRIVE_SERVICE_KEY")

# Настройки SMTP (для Gmail)
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 465

def extract_youtube_urls(text: str) -> list[str]:
    """Ищет ВСЕ ссылки на YouTube в тексте письма"""
    pattern = r"(https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)[0-9A-Za-z_-]{11})"
    return re.findall(pattern, text)

def get_emails_from_label(label_name="yt") -> list[dict]:
    """Проверяет письма внутри конкретного ярлыка/папки IMAP"""
    if not EMAIL_USER or not EMAIL_PASS:
        print("[-] Ошибка: Не заданы учетные данные почты.")
        return []

    tasks = []
    try:
        # Подключение по IMAP
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(EMAIL_USER, EMAIL_PASS)
        
        # Выбираем папку ярлыка (в Gmail ярлыки доступны как папки)
        status, _ = mail.select(f'"{label_name}"')
        if status != 'OK':
            # Если не удалось выбрать, пробуем без кавычек
            status, _ = mail.select(label_name)
            if status != 'OK':
                print(f"[-] Не удалось открыть ярлык/папку: {label_name}")
                mail.logout()
                return []

        # Ищем непрочитанные письма
        status, messages = mail.search(None, '(UNSEEN)')
        if status != 'OK' or not messages[0]:
            mail.logout()
            return []

        for num in messages[0].split():
            res, msg_data = mail.fetch(num, '(RFC822)')
            if res != 'OK':
                continue

            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    
                    # Извлекаем отправителя
                    sender = msg.get("From")
                    # Очищаем email от имени (например, "John <john@example.com>" -> "john@example.com")
                    sender_email = re.search(r'[\w\.-]+@[\w\.-]+', sender)
                    sender_address = sender_email.group(0) if sender_email else sender

                    # Извлекаем тело письма
                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() == "text/plain":
                                body = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                                break
                    else:
                        body = msg.get_payload(decode=True).decode('utf-8', errors='ignore')

                    yt_links = extract_youtube_urls(body)
                    if yt_links:
                        tasks.append({
                            "sender": sender_address,
                            "links": list(set(yt_links)) # Убираем дубликаты ссылок
                        })
                        # Помечаем письмо прочитанным
                        mail.store(num, '+FLAGS', '\\Seen')

        mail.logout()
    except Exception as e:
        print(f"[-] Ошибка работы с IMAP: {e}")

    return tasks

def download_via_cobalt(video_url: str, output_filename="video.mp4") -> bool:
    """Скачивает видео через Cobalt API в режиме стриминга"""
    api_url = "https://api.cobalt.tools/api/json"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json"
    }
    payload = {
        "url": video_url,
        "vCodec": "h264",
        "videoQuality": "max"
    }

    print(f"[*] Скачивание через Cobalt: {video_url}")
    try:
        res = requests.post(api_url, json=payload, headers=headers, timeout=15)
        if res.status_code == 200:
            data = res.json()
            status = data.get("status")
            download_url = None

            if status in ["redirect", "tunnel"]:
                download_url = data.get("url")
            elif status == "picker":
                picker = data.get("picker", [])
                if picker:
                    download_url = picker[0].get("url")

            if download_url:
                with requests.get(download_url, stream=True, timeout=60) as r:
                    r.raise_for_status()
                    with open(output_filename, 'wb') as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                return True
    except Exception as e:
        print(f"[-] Ошибка Cobalt API: {e}")
    
    return False

def upload_to_gdrive_and_get_direct_link(file_path: str) -> str | None:
    """Загружает файл на Google Диск, расшаривает его и делает прямую ссылку на автоскачивание"""
    if not SERVICE_KEY_JSON or not FOLDER_ID:
        print("[-] Ошибка: Отсутствуют ключи Google Drive.")
        return None

    try:
        key_dict = json.loads(SERVICE_KEY_JSON)
        creds = service_account.Credentials.from_service_account_info(
            key_dict, scopes=['https://www.googleapis.com/auth/drive']
        )
        
        service = build('drive', 'v3', credentials=creds)

        # 1. Загрузка файла
        file_metadata = {
            'name': os.path.basename(file_path),
            'parents': [FOLDER_ID]
        }
        media = MediaFileUpload(file_path, resumable=True)

        print("[*] Загрузка файла на Google Диск...")
        file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id'
        ).execute()

        file_id = file.get('id')

        # 2. Делаем файл публичным для чтения (чтобы работала ссылка)
        user_permission = {
            'type': 'anyone',
            'role': 'reader',
        }
        service.permissions().create(
            fileId=file_id,
            body=user_permission,
            fields='id',
        ).execute()

        # 3. Формируем прямую ссылку на скачивание
        direct_download_url = f"https://drive.google.com/uc?export=download&id={file_id}"
        return direct_download_url

    except Exception as e:
        print(f"[-] Ошибка Google Drive API: {e}")
        return None

def send_reply_email(to_email: str, direct_link: str):
    """Отправляет письмо с прямой ссылкой на файл"""
    try:
        msg = MIMEText(direct_link, 'plain', 'utf-8')
        msg['Subject'] = 'yt'
        msg['From'] = EMAIL_USER
        msg['To'] = to_email

        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
            server.login(EMAIL_USER, EMAIL_PASS)
            server.sendmail(EMAIL_USER, [to_email], msg.as_string())
            
        print(f"[+] Ответное письмо со ссылкой отправлено на {to_email}")
    except Exception as e:
        print(f"[-] Ошибка отправки письма через SMTP: {e}")

if __name__ == "__main__":
    print("[*] Сканирование ярлыка 'yt' на новые письма...")
    email_tasks = get_emails_from_label(label_name="yt")

    if not email_tasks:
        print("[-] Новых писем со ссылками в ярлыке 'yt' не найдено.")
        exit(0)

    for task in email_tasks:
        recipient = task["sender"]
        links = task["links"]
        
        print(f"\n[+] Обработка {len(links)} ссылок для {recipient}...")

        for idx, yt_url in enumerate(links):
            temp_filename = f"video_{int(time.time())}_{idx}.mp4"
            
            # Скачиваем во временный файл
            if download_via_cobalt(yt_url, temp_filename):
                # Загружаем и получаем прямую ссылку
                direct_url = upload_to_gdrive_and_get_direct_link(temp_filename)
                
                # Удаляем локальный файл
                if os.path.exists(temp_filename):
                    os.remove(temp_filename)

                if direct_url:
                    # Отправляем письмо с прямой ссылкой
                    send_reply_email(recipient, direct_url)
                    
                    # Интервал 2 секунды перед следующим файлом
                    if idx < len(links) - 1:
                        time.sleep(2)
            else:
                print(f"[-] Не удалось обработать ссылку: {yt_url}")
