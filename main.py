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

def get_video_title(video_url: str) -> str:
    """Получает оригинальное название видео с YouTube."""
    cmd = ["yt-dlp", "--get-title", "--no-warnings", video_url]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        title = res.stdout.strip()
        clean_title = re.sub(r'[\\/*?:"<>|]', "", title)
        return clean_title if clean_title else "video"
    except Exception:
        return "video"

def download_via_ytdlp(video_url: str, output_filename="video.mp4") -> tuple[bool, str]:
    """Скачивает видео strictly в FullHD/HD без скатывания в 360p/заглушки."""
    video_title = get_video_title(video_url)
    print(f"[*] Название видео: '{video_title}'")
    print(f"[*] Скачивание через yt-dlp для: {video_url}")
    
    client_strategies = [
        None,                        # Автовыбор yt-dlp
        "ios",                       # iOS клиент
        "tv_embedded"                # SmartTV клиент
    ]

    for attempt, client_group in enumerate(client_strategies, 1):
        print(f"[*] Попытка {attempt}/{len(client_strategies)} (Стратегия: {client_group or 'Auto'})...")
        
        if os.path.exists(output_filename):
            try:
                os.remove(output_filename)
            except Exception:
                pass

        cmd = [
            "yt-dlp",
            "--no-warnings",
            "--no-part",
            "-f", "bestvideo[height<=1080]+bestaudio/bestvideo+bestaudio",
            "--merge-output-format", "mp4",
            "-o", output_filename,
            video_url
        ]

        if client_group:
            cmd.extend(["--extractor-args", f"youtube:player_client={client_group}"])

        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            if result.returncode != 0:
                print(f"[-] yt-dlp вернул ошибку: {result.stderr.strip()[:200]}")
                raise Exception("yt-dlp завершился с ненулевым кодом.")

            if os.path.exists(output_filename):
                file_size_mb = os.path.getsize(output_filename) / (1024 * 1024)
                
                if file_size_mb < 2.0:
                    print(f"[-] Файл слишком мал ({file_size_mb:.2f} МБ). Это не полноценное видео.")
                    os.remove(output_filename)
                    raise Exception("Файл меньше 2 МБ.")

                print(f"[+] Видео успешно скачано! Размер: {file_size_mb:.2f} МБ")
                return True, video_title

        except Exception as e:
            print(f"[-] Сбой на попытке {attempt}: {e}")
            if os.path.exists(output_filename):
                try:
                    os.remove(output_filename)
                except Exception:
                    pass

            print("[!] Ротация IP через Cloudflare WARP...")
            try:
                subprocess.run(["warp-cli", "--accept-tos", "registration", "delete"], capture_output=True)
                subprocess.run(["warp-cli", "--accept-tos", "registration", "new"], check=True, capture_output=True)
                subprocess.run(["warp-cli", "--accept-tos", "connect"], check=True, capture_output=True)
                time.sleep(3)
            except Exception as warp_err:
                print(f"[-] Ошибка WARP: {warp_err}")

    return False, video_title

def upload_to_temporary_storage(file_path: str) -> str | None:
    """Загружает файл и формирует прямую ссылку для скачивания (Direct Download)."""
    
    # 1. Catbox.moe (Прямой статический CDN - идеален для Яндекс.Диска)
    print("[*] Загрузка файла на Catbox.moe...")
    try:
        with open(file_path, 'rb') as f:
            data = {'reqtype': 'fileupload'}
            files = {'fileToUpload': f}
            res = requests.post("https://catbox.moe/user/api.php", data=data, files=files, timeout=600)
            res.raise_for_status()
            url = res.text.strip()
            if url.startswith("https://"):
                print(f"[+] Прямая ссылка Catbox: {url}")
                return url
    except Exception as e:
        print(f"[-] Не удалось выгрузить на Catbox: {e}")

    # 2. Tmpfiles.org (Преобразуем ссылку в прямой скачиваемый поток /dl/)
    print("[*] Загрузка файла на Tmpfiles.org...")
    try:
        with open(file_path, 'rb') as f:
            res = requests.post("https://tmpfiles.org/api/v1/upload", files={'file': f}, timeout=600)
            res.raise_for_status()
            data = res.json()
            if data.get("status") == "success":
                url = data["data"]["url"]
                # ПРЕОБРАЗОВАНИЕ: https://tmpfiles.org/123/v.mp4 -> https://tmpfiles.org/dl/123/v.mp4
                direct_url = url.replace("tmpfiles.org/", "tmpfiles.org/dl/")
                print(f"[+] Прямая ссылка Tmpfiles: {direct_url}")
                return direct_url
    except Exception as e:
        print(f"[-] Не удалось выгрузить на Tmpfiles: {e}")

    # 3. Pixeldrain (Принудительный параметр ?download)
    print("[*] Загрузка файла на Pixeldrain...")
    try:
        with open(file_path, 'rb') as f:
            res = requests.post("https://pixeldrain.com/api/file", files={'file': f}, timeout=600)
            res.raise_for_status()
            data = res.json()
            if data.get("success"):
                file_id = data["id"]
                # ПРЕОБРАЗОВАНИЕ: добавляем ?download для принудительной отдачи файла
                direct_url = f"https://pixeldrain.com/api/file/{file_id}?download"
                print(f"[+] Прямая ссылка Pixeldrain: {direct_url}")
                return direct_url
    except Exception as e:
        print(f"[-] Не удалось выгрузить на Pixeldrain: {e}")

    return None

def send_reply_email(to_email: str, direct_link: str, video_title: str):
    """Отправляет письмо формата: <ссылка> <название_видео.mp4>"""
    try:
        email_body = f"{direct_link} {video_title}.mp4"
        
        msg = MIMEText(email_body, 'plain', 'utf-8')
        msg['Subject'] = 'yt'
        msg['From'] = SMTP_USER
        msg['To'] = to_email

        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, [to_email], msg.as_string())
            
        print(f"[+] Письмо успешно отправлено с {SMTP_USER} на {to_email}")
        print(f"[+] Текст письма: {email_body}")
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
                success, title = download_via_ytdlp(yt_url, temp_filename)
                if success:
                    public_url = upload_to_temporary_storage(temp_filename)
                    
                    if public_url:
                        send_reply_email(recipient, public_url, title)
                        
                        if idx < len(links) - 1:
                            time.sleep(2)
                else:
                    print(f"[-] Не удалось обработать ссылку: {yt_url}")
            finally:
                if os.path.exists(temp_filename):
                    os.remove(temp_filename)
                    print(f"[+] Временный файл {temp_filename} успешно удален из раннера GitHub.")
