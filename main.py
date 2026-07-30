import os
import imaplib
import smtplib
import email
from email.mime.text import MIMEText
import re
import time
import subprocess
import requests

# Переменные окружения из GitHub Secrets
EMAIL_USER = os.getenv("EMAIL_ACCOUNT")
EMAIL_PASS = os.getenv("EMAIL_PASSWORD")
YANDEX_TOKEN = os.getenv("YANDEX_DISK_TOKEN")

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

def upload_to_yandex_disk(file_path: str) -> str | None:
    """Загружает файл на Яндекс.Диск и возвращает публичную ссылку."""
    if not YANDEX_TOKEN:
        print("[-] Ошибка: Переменная YANDEX_DISK_TOKEN не задана.")
        return None

    filename = os.path.basename(file_path)
    ya_path = f"disk:/{filename}"
    headers = {"Authorization": f"OAuth {YANDEX_TOKEN}"}

    try:
        # 1. Запрашиваем одноразовую ссылку для загрузки
        print("[*] Запрос ссылки для загрузки на Яндекс.Диск...")
        upload_req = requests.get(
            "https://cloud-api.yandex.net/v1/disk/resources/upload",
            params={"path": ya_path, "overwrite": "true"},
            headers=headers
        )
        upload_req.raise_for_status()
        upload_url = upload_req.json().get("href")

        # 2. Выгружаем файл напрямую
        print("[*] Выгрузка файла на Яндекс.Диск...")
        with open(file_path, "rb") as f:
            put_req = requests.put(upload_url, data=f)
            put_req.raise_for_status()

        # 3. Публикуем файл (делаем доступным по ссылке)
        print("[*] Генерация публичной ссылки...")
        pub_req = requests.put(
            "https://cloud-api.yandex.net/v1/disk/resources/publish",
            params={"path": ya_path},
            headers=headers
        )
        pub_req.raise_for_status()

        # 4. Запрашиваем данные о созданном публичном файле
        meta_req = requests.get(
            "https://cloud-api.yandex.net/v1/disk/resources",
            params={"path": ya_path},
            headers=headers
        )
        meta_req.raise_for_status()
        public_url = meta_req.json().get("public_url")

        print(f"[+] Успешно! Ссылка: {public_url}")
        return public_url

    except Exception as e:
        print(f"[-] Ошибка Яндекс.Диск API: {e}")
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
            
            try:
                if download_via_ytdlp(yt_url, temp_filename):
                    public_url = upload_to_yandex_disk(temp_filename)
                    
                    if public_url:
                        send_reply_email(recipient, public_url)
                        
                        if idx < len(links) - 1:
                            time.sleep(2)
                else:
                    print(f"[-] Не удалось обработать ссылку: {yt_url}")
            finally:
                # Всегда зачищаем скачанный файл с раннера GitHub
                if os.path.exists(temp_filename):
                    os.remove(temp_filename)
                    print(f"[+] Временный файл {temp_filename} успешно удален из хранилища GitHub.")
