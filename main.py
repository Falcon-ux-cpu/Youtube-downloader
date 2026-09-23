import os
import re
import time
import base64
import urllib.parse
from flask import Flask, jsonify
import requests
import yt_dlp
from stem import Signal
from stem.control import Controller

app = Flask(__name__)

# --- КОНФИГУРАЦИЯ ---
KOOFR_WEBDAV_URL = os.getenv("KOOFR_WEBDAV_URL", "https://app.koofr.net/dav/Koofr/")
KOOFR_USER = os.getenv("KOOFR_USER")
KOOFR_PASS = os.getenv("KOOFR_PASS")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO")      # Формат: "owner/repo"
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
FILE_PATH_IN_REPO = "links.txt"

DOWNLOAD_DIR = "/tmp/downloads"
LINKS_FILE = os.path.join(os.path.dirname(__file__), "links.txt")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# --- НАСТРОЙКИ TOR PROXY ---
TOR_SOCKS_PROXY = os.getenv("TOR_SOCKS_PROXY", "socks5h://127.0.0.1:9050")
TOR_CONTROL_PORT = int(os.getenv("TOR_CONTROL_PORT", 9051))
TOR_PASSWORD = os.getenv("TOR_PASSWORD", "password")

PROXIES = {
    'http': TOR_SOCKS_PROXY,
    'https': TOR_SOCKS_PROXY
}


def renew_tor_ip():
    """Отправляет сигнал NEWNYM в Tor Control Port для получения нового IP."""
    try:
        with Controller.from_port(port=TOR_CONTROL_PORT) as controller:
            controller.authenticate(password=TOR_PASSWORD)
            controller.signal(Signal.NEWNYM)
            print("[TOR] Запрос на смену IP отправлен. Ожидание перестройки цепочки...")
            time.sleep(5)  # Задержка для применения нового IP
            
            # Проверка текущего IP через Tor
            res = requests.get("https://api.ipify.org?format=json", proxies=PROXIES, timeout=10)
            print(f"[TOR] Новый внешний IP: {res.json().get('ip')}")
            return True
    except Exception as e:
        print(f"[TOR ОШИБКА] Не удалось сменить IP через Tor: {e}")
        return False


def remove_line_from_github(line_to_remove):
    """Удаляет обработанную строку из links.txt напрямую в репозитории GitHub."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        print("[ВНИМАНИЕ] GITHUB_TOKEN или GITHUB_REPO не заданы. Пропуск обновления GitHub.")
        return False

    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{FILE_PATH_IN_REPO}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }

    try:
        # 1. Получаем текущее содержимое файла и sha
        res = requests.get(url, headers=headers, params={"ref": GITHUB_BRANCH}, timeout=15)
        if res.status_code != 200:
            print(f"[ОШИБКА GITHUB] Не удалось получить файл: {res.status_code} {res.text}")
            return False

        data = res.json()
        sha = data["sha"]
        content_bytes = base64.b64decode(data["content"])
        content_text = content_bytes.decode('utf-8')

        # 2. Фильтруем строки
        lines = [line.strip() for line in content_text.splitlines() if line.strip()]
        if line_to_remove in lines:
            lines.remove(line_to_remove)

        new_content = "\n".join(lines) + ("\n" if lines else "")
        encoded_content = base64.b64encode(new_content.encode('utf-8')).decode('utf-8')

        # 3. Отправляем обновленный файл обратно в GitHub
        payload = {
            "message": f"Auto-remove processed link: {line_to_remove[:30]}...",
            "content": encoded_content,
            "sha": sha,
            "branch": GITHUB_BRANCH
        }

        put_res = requests.put(url, headers=headers, json=payload, timeout=15)
        if put_res.status_code in (200, 201):
            print("[УСПЕХ GITHUB] Ссылка удалена из links.txt в репозитории.")
            return True
        else:
            print(f"[ОШИБКА GITHUB] Не удалось обновить файл: {put_res.status_code} {put_res.text}")
            return False

    except Exception as e:
        print(f"[ОШИБКА GITHUB] {e}")
        return False


def upload_to_koofr(file_path, remote_filename):
    """Загрузка файла на Koofr через WebDAV с удалением при успехе."""
    target_url = urllib.parse.urljoin(KOOFR_WEBDAV_URL, urllib.parse.quote(remote_filename))
    
    print(f"--> Загрузка {remote_filename} на Koofr...")
    try:
        with open(file_path, 'rb') as f:
            # Загрузку на Koofr выполняем напрямую (без Tor) для максимальной скорости
            response = requests.put(
                target_url,
                auth=(KOOFR_USER, KOOFR_PASS),
                data=f,
                headers={'Content-Type': 'application/octet-stream'},
                timeout=300
            )
        
        if response.status_code in (200, 201, 204):
            print(f"[УСПЕХ] Файл {remote_filename} загружен на Koofr.")
            os.remove(file_path)
            print(f"[ОЧИСТКА] Локальный файл {file_path} удален.")
            return True
        else:
            print(f"[ОШИБКА] Koofr ответил кодом {response.status_code}: {response.text}")
            return False
    except Exception as e:
        print(f"[ОШИБКА KOOFR] {e}")
        return False


def download_content(url, quality, remote_filename):
    """Скачивание медиа по URL через Tor SOCKS5 прокси."""
    filename = os.path.join(DOWNLOAD_DIR, remote_filename)
    
    if quality.isdigit():
        fmt = f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best"
    else:
        fmt = "bestvideo+bestaudio/best"

    ydl_opts = {
        'outtmpl': filename,
        'quiet': True,
        'no_warnings': True,
        'format': fmt,
        'merge_output_format': 'mp4' if filename.endswith('.mp4') else None,
        'proxy': TOR_SOCKS_PROXY,  # Проксирование yt-dlp через Tor
    }

    try:
        print(f"[DOWNLOAD] Скачивание через yt-dlp (Tor proxy: {TOR_SOCKS_PROXY})...")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
            if os.path.exists(filename):
                return filename
            base, _ = os.path.splitext(filename)
            if os.path.exists(base + ".mp4"):
                return base + ".mp4"
    except Exception as e:
        print(f"yt-dlp не справился ({e}), пробуем прямое скачивание через Tor SOCKS5...")
        
        # Резервный фоллбек через requests + Tor
        res = requests.get(url, stream=True, proxies=PROXIES, timeout=30)
        res.raise_for_status()
        
        with open(filename, 'wb') as f:
            for chunk in res.iter_content(chunk_size=8192):
                f.write(chunk)
        return filename

    return filename


def process_links():
    """Чтение файла links.txt, скачивание, отправка на Koofr и удаление ссылки из GitHub."""
    if not os.path.exists(LINKS_FILE):
        print(f"[ИНФО] Файл {LINKS_FILE} не найден.")
        return 0

    with open(LINKS_FILE, 'r', encoding='utf-8') as f:
        lines = [line.strip() for line in f if line.strip()]

    if not lines:
        print("[ИНФО] Файл links.txt пуст.")
        return 0

    processed_count = 0

    for line in list(lines):
        parts = line.split(maxsplit=2)
        if len(parts) < 3:
            print(f"[ПРОПУСК] Неверный формат строки: {line}")
            continue

        url, quality, remote_filename = parts[0].strip(), parts[1].strip(), parts[2].strip()
        print(f"\n==========================================")
        print(f"Обработка: URL={url}, Качество={quality}, Имя={remote_filename}")

        # Сменяем IP перед каждой новой ссылкой
        renew_tor_ip()

        try:
            # 1. Скачивание через Tor
            local_file = download_content(url, quality, remote_filename)

            if local_file and os.path.exists(local_file):
                # 2. Загрузка на Koofr (напрямую) и удаление локального файла
                actual_remote_name = os.path.basename(local_file)
                if upload_to_koofr(local_file, actual_remote_name):
                    processed_count += 1
                    
                    # 3. Удаляем обработанную строку из GitHub
                    remove_line_from_github(line)

        except Exception as err:
            print(f"[ОШИБКА ОБРАБОТКИ] {err}")

    return processed_count


@app.route("/")
def index():
    return "Worker with Tor integration is active", 200


@app.route("/trigger")
def trigger():
    """Эндпоинт для внешнего вызова (cron)."""
    try:
        count = process_links()
        return jsonify({"status": "success", "processed_files": count}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
