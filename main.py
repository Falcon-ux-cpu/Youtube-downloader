import os
import json
import imaplib
import smtplib
import email
from email.mime.text import MIMEText
import re
import time
import subprocess
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Переменные окружения из GitHub Secrets
EMAIL_USER = os.getenv("EMAIL_ACCOUNT")
EMAIL_PASS = os.getenv("EMAIL_PASSWORD")
FOLDER_ID = os.getenv("GDRIVE_FOLDER_ID")
SERVICE_KEY_JSON = os.getenv("GDRIVE_SERVICE_KEY")

# Настройки SMTP для Gmail
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 465

def extract_youtube_urls(text: str) -> list[str]:
    """Ищет все уникальные ссылки на YouTube в тексте письма."""
    pattern = r"(https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)[0-9A-Za-z_-]{11})"
    return list(set(re.findall(pattern, text)))

def get_emails_from_label(label_name="yt") -> list[dict]:
    """Проверяет непрочитанные письма в ярлыке 'yt' через IMAP."""
    if not EMAIL_USER or not EMAIL_PASS:
        print("[-] Ошибка: Переменные EMAIL_ACCOUNT или EMAIL_PASSWORD не заданы.")
        return []

    tasks = []
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(EMAIL_USER, EMAIL_PASS)
        
        status, _ = mail.select(f'"{label_name}"')
        if status != 'OK':
            status, _ = mail.select(label_name)
            if status != 'OK':
                print(f"[-] Не удалось открыть ярлык IMAP: {label_name}")
                mail.logout()
                return []

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
                    
                    sender = msg.get("From", "")
                    sender_match = re.search(r'[\w\.-]+@[\w\.-]+', sender)
                    sender_address = sender_match.group(0) if sender_match else sender

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
                            "links": yt_links
                        })
                        mail.store(num, '+FLAGS', '\\Seen')

        mail.logout()
    except Exception as e:
        print(f"[-] Ошибка работы с IMAP: {e}")

    return tasks

def download_via_ytdlp(video_url: str, output_filename="video.mp4") -> bool:
    """Загружает видео через yt-dlp с ротацией IP в Cloudflare WARP при блокировке."""
    print(f"[*] Скачивание через yt-dlp для: {video_url}")
    
    player_clients = [
        "ios,android",
        "mweb,web_embedded",
        "tv_embedded,android"
    ]

    for attempt, client_group in enumerate(player_clients, 1):
        print(f"[*] Попытка {attempt}/{len(player_clients)} с клиентом [{client_group}]...")
        
        cmd = [
            "yt-dlp",
            "--no-warnings",
            "--format", "b[ext=mp4]/best[ext=mp4]/best",
            "--extractor-args", f"youtube:player_client={client_group}",
            "-o", output_filename,
            video_url
        ]

        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True)
            print(f"[+] Файл успешно скачан во временную директорию: {output_filename}")
            return True
        except subprocess.CalledProcessError as e:
            err_msg = e.stderr if e.stderr else e.stdout
            print(f"[-] Ошибка на попытке {attempt}: {err_msg.strip()}")
            
            if any(term in err_msg for term in ["not a bot", "429", "Sign in"]):
                print("[!] Обнаружена защита от ботов. Ротация IP через Cloudflare WARP...")
                try:
                    subprocess.run(["warp-cli", "--accept-tos", "registration", "new"], check=True, capture_output=True)
                    subprocess.run(["warp-cli", "--accept-tos", "connect"], check=True, capture_output=True)
                    time.sleep(3)
                except Exception as warp_err:
                    print(f"[-] Не удалось ротировать WARP: {warp_err}")

    return False

def upload_to_gdrive_and_get_direct_link(file_path: str, owner_email: str) -> str | None:
    """
    Загружает видео на Google Диск, передаёт владение на основной аккаунт 
    и генерирует прямую ссылку на скачивание.
    """
    if not SERVICE_KEY_JSON or not FOLDER_ID:
        print("[-] Ошибка: Отсутствуют GDRIVE_SERVICE_KEY или GDRIVE_FOLDER_ID.")
        return None

    try:
        key_dict = json.loads(SERVICE_KEY_JSON)
        creds = service_account.Credentials.from_service_account_info(
            key_dict, scopes=['https://www.googleapis.com/auth/drive']
        )
        
        service = build('drive', 'v3', credentials=creds)

        file_metadata = {
            'name': os.path.basename(file_path),
            'parents': [FOLDER_ID]
        }
        media = MediaFileUpload(file_path, resumable=True)

        print("[*] Загрузка файла на Google Диск...")
        file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id',
            supportsAllDrives=True
        ).execute()

        file_id = file.get('id')

        # 1. Открываем публичный доступ на чтение по ссылке
        user_permission = {
            'type': 'anyone',
            'role': 'reader',
        }
        service.permissions().create(
            fileId=file_id,
            body=user_permission,
            fields='id',
            supportsAllDrives=True
        ).execute()

        # 2. Передаем владение файлом твоему личного аккаунту (списывает квоту с тебя)
        if owner_email:
            try:
                owner_permission = {
                    'type': 'user',
                    'role': 'owner',
                    'emailAddress': owner_email
                }
                service.permissions().create(
                    fileId=file_id,
                    body=owner_permission,
                    transferOwnership=True,
                    supportsAllDrives=True
                ).execute()
            except Exception as transfer_err:
                print(f"[!] Предупреждение при передаче прав владельца: {transfer_err}")

        return f"https://drive.google.com/uc?export=download&id={file_id}"

    except Exception as e:
        print(f"[-] Ошибка Google Drive API: {e}")
        return None

def send_reply_email(to_email: str, direct_link: str):
    """Отправляет ответное письмо со ссылкой на файл."""
    try:
        msg = MIMEText(direct_link, 'plain', 'utf-8')
        msg['Subject'] = 'yt'
        msg['From'] = EMAIL_USER
        msg['To'] = to_email

        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
            server.login(EMAIL_USER, EMAIL_PASS)
            server.sendmail(EMAIL_USER, [to_email], msg.as_string())
            
        print(f"[+] Ответное письмо отправлено на {to_email}")
    except Exception as e:
        print(f"[-] Ошибка отправки по SMTP: {e}")

if __name__ == "__main__":
    print("[*] Поиск писем в ярлыке 'yt'...")
    email_tasks = get_emails_from_label(label_name="yt")

    if not email_tasks:
        print("[-] Новых ссылок в ярлыке 'yt' нет.")
        exit(0)

    for task in email_tasks:
        recipient = task["sender"]
        links = task["links"]
        
        print(f"\n[+] Обработка {len(links)} ссылок для адреса {recipient}...")

        for idx, yt_url in enumerate(links):
            temp_filename = f"video_{int(time.time())}_{idx}.mp4"
            
            if download_via_ytdlp(yt_url, temp_filename):
                direct_url = upload_to_gdrive_and_get_direct_link(temp_filename, owner_email=EMAIL_USER)
                
                if os.path.exists(temp_filename):
                    os.remove(temp_filename)

                if direct_url:
                    send_reply_email(recipient, direct_url)
                    
                    if idx < len(links) - 1:
                        time.sleep(2)
            else:
                print(f"[-] Не удалось обработать ссылку: {yt_url}")
