import os
import imaplib
import smtplib
import email
from email.mime.text import MIMEText
import re
import time
import subprocess
import requests

# Почта для чтения входящих писем (IMAP)
IMAP_USER = os.getenv("EMAIL_ACCOUNT")
IMAP_PASS = os.getenv("EMAIL_PASSWORD")

# Почта для отправки ответных писем (SMTP) — если не задана отдельно, берем IMAP аккаунт
SMTP_USER = os.getenv("SENDER_EMAIL_ACCOUNT", IMAP_USER)
SMTP_PASS = os.getenv("SENDER_EMAIL_PASSWORD", IMAP_PASS)

# Фиксированный получатель уведомлений (опционально)
TARGET_EMAIL = os.getenv("TARGET_NOTIFICATION_EMAIL")

# Настройки SMTP для Gmail
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 465

def extract_youtube_urls(text: str) -> list[str]:
    """Ищет все уникальные ссылки на YouTube в тексте письма."""
    pattern = r"(https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)[0-9A-Za-z_-]{11})"
    return list(set(re.findall(pattern, text)))

def get_emails_from_label(label_name="yt") -> list[dict]:
    """Проверяет непрочитанные письма в ярлыке 'yt' через IMAP."""
    if not IMAP_USER or not IMAP_PASS:
        print("[-] Ошибка: Переменные EMAIL_ACCOUNT или EMAIL_PASSWORD не заданы.")
        return []

    tasks = []
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(IMAP_USER, IMAP_PASS)
        
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

def upload_to_temporary_storage(file_path: str) -> str | None:
    """Загружает файл на Catbox.moe, а при сбое — на Gofile.io (с извлечением прямой ссылки)."""
    
    # 1. Пробуем Catbox.moe (Прямая ссылка)
    print("[*] Загрузка файла на Catbox.moe...")
    try:
        with open(file_path, 'rb') as f:
            data = {'reqtype': 'fileupload'}
            files = {'fileToUpload': f}
            res = requests.post("https://catbox.moe/user/api.php", data=data, files=files, timeout=300)
            res.raise_for_status()
            
            url = res.text.strip()
            if url.startswith("https://"):
                print(f"[+] Файл успешно выгружен на Catbox: {url}")
                return url
    except Exception as e:
        print(f"[-] Не удалось выгрузить на Catbox: {e}")

    # 2. Фолбэк на Gofile.io (Прямая ссылка на файл)
    print("[*] Переключение на Gofile.io...")
    try:
        server_res = requests.get("https://api.gofile.io/servers", timeout=30).json()
        if server_res.get("status") == "ok":
            server = server_res["data"]["servers"][0]["name"]
            upload_url = f"https://{server}.gofile.io/contents/uploadfile"
            
            with open(file_path, 'rb') as f:
                files = {'file': f}
                res = requests.post(upload_url, files=files, timeout=600)
                res.raise_for_status()
                
                result = res.json()
                if result.get("status") == "ok":
                    # Достаем прямую ссылку на файл для Яндекс.Диска
                    files_data = result["data"].get("files", {})
                    if files_data:
                        first_file_key = list(files_data.keys())[0]
                        direct_link = files_data[first_file_key].get("link")
                        if direct_link:
                            print(f"[+] Прямая ссылка Gofile: {direct_link}")
                            return direct_link

                    # Если не вышло получить прямую — возвращаем ссылку на страницу
                    download_page = result["data"]["downloadPage"]
                    print(f"[+] Выгружено на Gofile (страница): {download_page}")
                    return download_page
    except Exception as e:
        print(f"[-] Не удалось выгрузить на Gofile: {e}")

    return None

def send_reply_email(to_email: str, direct_link: str):
    """Отправляет письмо с указанного SMTP-аккаунта получателю."""
    try:
        msg = MIMEText(f"Ссылка для скачивания файла:\n{direct_link}", 'plain', 'utf-8')
        msg['Subject'] = 'yt'
        msg['From'] = SMTP_USER
        msg['To'] = to_email

        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, [to_email], msg.as_string())
            
        print(f"[+] Письмо успешно отправлено с {SMTP_USER} на {to_email}")
    except Exception as e:
        print(f"[-] Ошибка отправки по SMTP: {e}")

if __name__ == "__main__":
    print("[*] Поиск писем в ярлыке 'yt'...")
    email_tasks = get_emails_from_label(label_name="yt")

    if not email_tasks:
        print("[-] Новых ссылок в ярлыке 'yt' нет.")
        exit(0)

    for task in email_tasks:
        recipient = TARGET_EMAIL if TARGET_EMAIL else task["sender"]
        links = task["links"]
        
        print(f"\n[+] Обработка {len(links)} ссылок. Получатель: {recipient}...")

        for idx, yt_url in enumerate(links):
            temp_filename = f"video_{int(time.time())}_{idx}.mp4"
            
            try:
                if download_via_ytdlp(yt_url, temp_filename):
                    public_url = upload_to_temporary_storage(temp_filename)
                    
                    if public_url:
                        send_reply_email(recipient, public_url)
                        
                        if idx < len(links) - 1:
                            time.sleep(2)
                else:
                    print(f"[-] Не удалось обработать ссылку: {yt_url}")
            finally:
                if os.path.exists(temp_filename):
                    os.remove(temp_filename)
                    print(f"[+] Временный файл {temp_filename} успешно удален из раннера GitHub.")
