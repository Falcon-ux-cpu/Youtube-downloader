import os
import imaplib
import smtplib
import email
from email.mime.text import MIMEText
import re
import time
import subprocess
import requests

# Переменные окружения
IMAP_USER = os.getenv("EMAIL_ACCOUNT")
IMAP_PASS = os.getenv("EMAIL_PASSWORD")
SMTP_USER = os.getenv("SENDER_EMAIL_ACCOUNT", IMAP_USER)
SMTP_PASS = os.getenv("SENDER_EMAIL_PASSWORD", IMAP_PASS)
TARGET_EMAIL = os.getenv("TARGET_NOTIFICATION_EMAIL")
YANDEX_DISK_TOKEN = os.getenv("YANDEX_DISK_TOKEN")

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
    """Скачивает видео с защитой от скатывания в 360p."""
    video_title = get_video_title(video_url)
    print(f"[*] Название видео: '{video_title}'")
    print(f"[*] Скачивание через yt-dlp для: {video_url}")
    
    # iOS / TV клиенты отдают HD без блокировок ботов
    client_strategies = [
        "ios",
        "tv_embedded",
        "web_creator",
        None
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
            # Принудительно запрашиваем качество не ниже 720p
            "-f", "bestvideo[height<=1080][height>=720]+bestaudio/best[height>=720]/best",
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
                    print(f"[-] Файл слишком мал ({file_size_mb:.2f} МБ).")
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
    """Загружает файл и вытаскивает прямую ссылку из HTML блока тега <a class="download"> без использования bs4."""
    print("[*] Загрузка файла на Tmpfiles.org...")
    try:
        with open(file_path, 'rb') as f:
            res = requests.post("https://tmpfiles.org/api/v1/upload", files={'file': f}, timeout=600)
            res.raise_for_status()
            data = res.json()
            if data.get("status") == "success":
                page_url = data["data"]["url"]
                print(f"[*] Страница скачивания: {page_url}")
                
                # Получаем HTML-код страницы
                page_res = requests.get(page_url, timeout=30)
                if page_res.status_code == 200:
                    # Ищем тег <a class="download" href="..."> через регулярное выражение
                    match = re.search(r'<a\s+class="download"\s+href="(https://tmpfiles\.org/dl/[^"]+)"', page_res.text)
                    if not match:
                        # Запасной поиск любой href c /dl/
                        match = re.search(r'href="(https://tmpfiles\.org/dl/[^"]+)"', page_res.text)

                    if match:
                        direct_url = match.group(1)
                        print(f"[+] Прямая ссылка на файл из HTML: {direct_url}")
                        return direct_url

    except Exception as e:
        print(f"[-] Ошибка получения ссылки Tmpfiles: {e}")

    # 2. Резервный вариант: Pixeldrain
    print("[*] Резервная загрузка на Pixeldrain...")
    try:
        with open(file_path, 'rb') as f:
            res = requests.post("https://pixeldrain.com/api/file", files={'file': f}, timeout=600)
            res.raise_for_status()
            data = res.json()
            if data.get("success"):
                file_id = data["id"]
                direct_url = f"https://pixeldrain.com/api/file/{file_id}?download"
                print(f"[+] Прямая ссылка Pixeldrain: {direct_url}")
                return direct_url
    except Exception as e:
        print(f"[-] Не удалось выгрузить на Pixeldrain: {e}")

    return None

def upload_url_to_yandex_disk(download_url: str, video_title: str) -> bool:
    """Отправляет команду Яндекс.Диску и ОЖИДАЕТ полного завершения скачивания серверами Яндекса."""
    if not YANDEX_DISK_TOKEN:
        print("[-] YANDEX_DISK_TOKEN не задан. Загрузка на Яндекс.Диск пропущена.")
        return False

    headers = {
        "Authorization": f"OAuth {YANDEX_DISK_TOKEN}"
    }

    save_path = f"disk:/Share/{video_title}.mp4"

    params = {
        "url": download_url,
        "path": save_path
    }

    print(f"[*] Передача прямой ссылки Яндекс.Диску: {download_url}")
    try:
        res = requests.post("https://cloud-api.yandex.net/v1/disk/resources/upload", headers=headers, params=params, timeout=30)
        
        if res.status_code == 202:
            status_url = res.json().get("href")
            print(f"[+] Яндекс.Диск принял задачу! Ожидаем завершения скачивания...")
            
            for _ in range(120):
                time.sleep(5)
                check_res = requests.get(status_url, headers=headers, timeout=10)
                if check_res.status_code == 200:
                    status_data = check_res.json()
                    status = status_data.get("status")
                    
                    if status == "success":
                        print(f"[+] УСПЕХ: Файл сохранен на Яндекс.Диск по пути '{save_path}'!")
                        return True
                    elif status == "failed":
                        print(f"[-] Ошибка: Яндекс.Диск не смог скачать файл по переданной ссылке.")
                        return False
                    else:
                        print(f"[*] Скачивание Яндекс.Диском в процессе (статус: {status})...")
            
            print("[-] Превышено время ожидания загрузки на Яндекс.Диск.")
            return False
        else:
            print(f"[-] Яндекс.Диск вернул ошибку ({res.status_code}): {res.text}")
            return False
            
    except Exception as e:
        print(f"[-] Ошибка обращения к API Яндекс.Диска: {e}")
        return False

def send_reply_email(to_email: str, video_title: str):
    """Отправка уведомления об успешном сохранении на Яндекс.Диск."""
    try:
        email_body = f"Видео '{video_title}' успешно сохранено на ваш Яндекс.Диск (папка /Share)."
        
        msg = MIMEText(email_body, 'plain', 'utf-8')
        msg['Subject'] = 'yt -> Yandex.Disk'
        msg['From'] = SMTP_USER
        msg['To'] = to_email

        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, [to_email], msg.as_string())
            
        print(f"[+] Уведомление отправлено на {to_email}")
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
                    # 1. Загрузка во временное хранилище и вытаскивание прямой ссылки через regex
                    public_url = upload_to_temporary_storage(temp_filename)
                    
                    if public_url:
                        # 2. Передача ссылки на Яндекс.Диск
                        yd_success = upload_url_to_yandex_disk(public_url, title)
                        
                        # 3. Отправляем уведомление о сохранении на диск (без публичных ссылок)
                        if yd_success:
                            send_reply_email(recipient, title)
                        
                        if idx < len(links) - 1:
                            time.sleep(2)
                else:
                    print(f"[-] Не удалось обработать ссылку: {yt_url}")
            finally:
                if os.path.exists(temp_filename):
                    os.remove(temp_filename)
                    print(f"[+] Временный файл {temp_filename} удален из раннера.")
