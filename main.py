import os
import imaplib
import email
import re
import time
import subprocess
import requests

# Переменные окружения из GitHub Secrets
IMAP_USER = os.getenv("EMAIL_ACCOUNT")
IMAP_PASS = os.getenv("EMAIL_PASSWORD")
YANDEX_DISK_TOKEN = os.getenv("YANDEX_DISK_TOKEN")

# Локальный эндпоинт PO Token Provider
POT_PROVIDER_URL = "http://127.0.0.1:4444/get_pot"

def extract_youtube_urls(text: str) -> list[str]:
    """Ищет все уникальные ссылки на YouTube в тексте письма."""
    pattern = r"(https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)[0-9A-Za-z_-]{11})"
    return list(set(re.findall(pattern, text)))

def rotate_warp_ip():
    """Переподключает Cloudflare WARP для смены внешнего IP."""
    print("[!] Ротация IP через Cloudflare WARP...")
    try:
        subprocess.run(["warp-cli", "--accept-tos", "disconnect"], capture_output=True)
        time.sleep(2)
        subprocess.run(["warp-cli", "--accept-tos", "connect"], capture_output=True)
        time.sleep(4)
    except Exception as e:
        print(f"[-] Ошибка при переподключении WARP: {e}")

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
    """Получает название видео через yt-dlp с использованием PO Token."""
    cmd = [
        "yt-dlp",
        "--get-title",
        "--no-warnings",
        "--extractor-args", f"youtube:po_token=web+{POT_PROVIDER_URL}",
        video_url
    ]

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        title = res.stdout.strip()
        clean_title = re.sub(r'[\\/*?:"<>|]', "", title)
        return clean_title if clean_title else "video"
    except Exception:
        return "video"

def download_via_ytdlp(video_url: str, output_filename="video.mp4") -> tuple[bool, str]:
    """Скачивает видео: сочетает PO Token и повторные попытки с ротацией WARP."""
    video_title = get_video_title(video_url)
    print(f"[*] Название видео: '{video_title}'")
    print(f"[*] Скачивание через yt-dlp для: {video_url}")

    cmd = [
        "yt-dlp",
        "--no-warnings",
        "--no-part",
        "--extractor-args", f"youtube:po_token=web+{POT_PROVIDER_URL}",
        "-f", "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
        "--merge-output-format", "mp4",
        "-o", output_filename,
        video_url
    ]

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        if os.path.exists(output_filename):
            try:
                os.remove(output_filename)
            except Exception:
                pass

        print(f"[*] Попытка {attempt}/{max_retries}...")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            if result.returncode == 0 and os.path.exists(output_filename):
                file_size_mb = os.path.getsize(output_filename) / (1024 * 1024)
                if file_size_mb >= 0.5:
                    print(f"[+] УСПЕХ! Видео скачано! Размер: {file_size_mb:.2f} МБ")
                    return True, video_title
                else:
                    print(f"[-] Файл слишком мал ({file_size_mb:.2f} МБ). Повтор...")

            print(f"[-] yt-dlp вернул ошибку: {result.stderr.strip()[:200]}")
            
        except Exception as e:
            print(f"[-] Ошибка выполнения yt-dlp: {e}")

        if attempt < max_retries:
            rotate_warp_ip()

    return False, video_title

def upload_to_temporary_storage(file_path: str) -> str | None:
    """Загружает файл на Tmpfiles.org (основной) или Pixeldrain (резервный)."""
    print("[*] Загрузка файла на Tmpfiles.org...")
    try:
        with open(file_path, 'rb') as f:
            res = requests.post("https://tmpfiles.org/api/v1/upload", files={'file': f}, timeout=600)
            res.raise_for_status()
            data = res.json()
            if data.get("status") == "success":
                page_url = data["data"]["url"]
                
                page_res = requests.get(page_url, timeout=30)
                if page_res.status_code == 200:
                    match = re.search(r'<a\s+class="download"\s+href="(https://tmpfiles\.org/dl/[^"]+)"', page_res.text)
                    if not match:
                        match = re.search(r'href="(https://tmpfiles\.org/dl/[^"]+)"', page_res.text)

                    if match:
                        direct_url = match.group(1)
                        print(f"[+] Прямая ссылка Tmpfiles: {direct_url}")
                        return direct_url
    except Exception as e:
        print(f"[-] Ошибка Tmpfiles: {e}")

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
        print(f"[-] Ошибка Pixeldrain: {e}")

    return None

def upload_url_to_yandex_disk(download_url: str, video_title: str) -> bool:
    """Передает ссылку на фоновую загрузку в Яндекс.Диск."""
    if not YANDEX_DISK_TOKEN:
        print("[-] YANDEX_DISK_TOKEN не задан.")
        return False

    headers = {"Authorization": f"OAuth {YANDEX_DISK_TOKEN}"}
    save_path = f"disk:/Share/{video_title}.mp4"
    params = {"url": download_url, "path": save_path}

    try:
        res = requests.post("https://cloud-api.yandex.net/v1/disk/resources/upload", headers=headers, params=params, timeout=30)
        if res.status_code == 202:
            status_url = res.json().get("href")
            print(f"[+] Задача отправлена на Яндекс.Диск. Ожидание завершения...")
            
            for _ in range(120):
                time.sleep(5)
                check_res = requests.get(status_url, headers=headers, timeout=10)
                if check_res.status_code == 200:
                    status = check_res.json().get("status")
                    if status == "success":
                        print(f"[+] УСПЕХ: Загружено в '{save_path}'!")
                        return True
                    elif status == "failed":
                        print("[-] Ошибка Яндекс.Диска при скачивании файла.")
                        return False
            return False
        else:
            print(f"[-] Яндекс.Диск вернул код {res.status_code}: {res.text}")
            return False
    except Exception as e:
        print(f"[-] Ошибка Яндекс.Диска: {e}")
        return False

if __name__ == "__main__":
    print("[*] Поиск писем в ярлыке 'yt'...")
    email_tasks = get_emails_from_label(label_name="yt")

    if not email_tasks:
        print("[-] Новых ссылок нет.")
        exit(0)

    for task in email_tasks:
        links = task["links"]
        print(f"\n[+] Найдено ссылок: {len(links)}...")

        for idx, yt_url in enumerate(links):
            temp_filename = f"video_{int(time.time())}_{idx}.mp4"
            try:
                success, title = download_via_ytdlp(yt_url, temp_filename)
                if success:
                    public_url = upload_to_temporary_storage(temp_filename)
                    if public_url:
                        upload_url_to_yandex_disk(public_url, title)
                        if idx < len(links) - 1:
                            time.sleep(2)
            finally:
                if os.path.exists(temp_filename):
                    os.remove(temp_filename)
