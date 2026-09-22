import os
import re
import urllib.parse
from flask import Flask, jsonify
from imap_tools import MailBox, AND
import requests
import yt_dlp

app = Flask(__name__)

# Конфигурация из переменных окружения
IMAP_SERVER = os.getenv("IMAP_SERVER", "imap.gmail.com")
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")
SENDER_EMAIL = os.getenv("SENDER_EMAIL")

KOOFR_WEBDAV_URL = os.getenv("KOOFR_WEBDAV_URL", "https://app.koofr.net/dav/Koofr/")
KOOFR_USER = os.getenv("KOOFR_USER")
KOOFR_PASS = os.getenv("KOOFR_PASS")

DOWNLOAD_DIR = "/tmp/downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


def upload_to_koofr(file_path, remote_filename):
    """Загрузка файла на Koofr через WebDAV с удалением при успехе."""
    target_url = urllib.parse.urljoin(KOOFR_WEBDAV_URL, urllib.parse.quote(remote_filename))
    
    print(f"--> Загрузка {remote_filename} на Koofr...")
    with open(file_path, 'rb') as f:
        response = requests.put(
            target_url,
            auth=(KOOFR_USER, KOOFR_PASS),
            data=f,
            headers={'Content-Type': 'application/octet-stream'}
        )
    
    if response.status_code in (200, 201, 204):
        print(f"[УСПЕХ] Файл {remote_filename} загружен на Koofr.")
        os.remove(file_path)
        print(f"[ОЧИСТКА] Локальный файл {file_path} удален.")
        return True
    else:
        print(f"[ОШИБКА] Koofr ответил кодом {response.status_code}: {response.text}")
        return False


def download_content(url, custom_name):
    """Скачивание медиа по URL (поддерживает yt-dlp и прямые ссылки)."""
    # Шаблон для yt-dlp, чтобы сохранилось оригинальное расширение
    out_template = os.path.join(DOWNLOAD_DIR, f"{custom_name}.%(ext)s")
    
    ydl_opts = {
        'outtmpl': out_template,
        'quiet': True,
        'no_warnings': True,
        'format': 'bestvideo+bestaudio/best',
        'merge_output_format': 'mp4'
    }

    try:
        # Пробуем через yt-dlp (для видео-хостингов и медиа)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            # В случае merge_output_format расширение может измениться на mp4
            if not os.path.exists(filename):
                base, _ = os.path.splitext(filename)
                if os.path.exists(base + ".mp4"):
                    filename = base + ".mp4"
            return filename
    except Exception as e:
        print(f"yt-dlp не справился ({e}), пробуем прямое скачивание...")
        
        # Запасной вариант — прямое скачивание файла по HTTP
        res = requests.get(url, stream=True)
        res.raise_for_status()
        
        # Пытаемся определить расширение из заголовка или URL
        ext = ".bin"
        content_disp = res.headers.get('content-disposition', '')
        if 'filename=' in content_disp:
            ext = os.path.splitext(re.findall('filename="?([^"]+)"?', content_disp)[0])[1]
        else:
            path_ext = os.path.splitext(urllib.parse.urlparse(url).path)[1]
            if path_ext:
                ext = path_ext

        filename = os.path.join(DOWNLOAD_DIR, f"{custom_name}{ext}")
        with open(filename, 'wb') as f:
            for chunk in res.iter_content(chunk_size=8192):
                f.write(chunk)
        return filename


def process_emails():
    """Основной цикл парсинга почты и обработки задач."""
    processed_count = 0
    
    with MailBox(IMAP_SERVER).login(EMAIL_USER, EMAIL_PASS) as mb:
        # Ищем непрочитанные письма от конкретного отправителя
        messages = mb.fetch(AND(seen=False, from_=SENDER_EMAIL))
        
        for msg in messages:
            body = msg.text.strip()
            print(f"\nПолучено письмо ID {msg.uid}: {body}")
            
            # Парсинг: Ссылка + Пробел + Желаемое имя файла
            parts = body.split(maxsplit=1)
            if len(parts) < 2:
                print("[ПРОПУСК] Формат тела письма не отвечает условию 'URL ИМЯ_ФАЙЛА'")
                continue
                
            url, custom_name = parts[0].strip(), parts[1].strip()
            
            try:
                # 1. Скачивание
                local_file = download_content(url, custom_name)
                
                if local_file and os.path.exists(local_file):
                    ext = os.path.splitext(local_file)[1]
                    remote_name = f"{custom_name}{ext}"
                    
                    # 2. Загрузка на Koofr и удаление
                    if upload_to_koofr(local_file, remote_name):
                        # Помечаем письмо как прочитанное только после успешной загрузки
                        mb.flag(msg.uid, imap_tools.MailMessageFlags.SEEN, True)
                        processed_count += 1
            except Exception as err:
                print(f"[ОШИБКА ОБРАБОТКИ] {err}")

    return processed_count


@app.route("/")
def index():
    return "Worker is active", 200


@app.route("/trigger")
def trigger():
    """Эндпоинт для внешнего вызова (cron)."""
    try:
        count = process_emails()
        return jsonify({"status": "success", "processed_emails": count}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
