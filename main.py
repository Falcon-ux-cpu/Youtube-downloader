import os
import re
import imaplib
import email
from email.header import decode_header
import urllib.request
from flask import Flask, jsonify
from webdav4.client import Client

app = Flask(__name__)

# Параметры из переменных окружения
IMAP_SERVER = os.environ.get("IMAP_SERVER", "imap.gmail.com")
EMAIL_USER = os.environ.get("EMAIL_USER")
EMAIL_PASS = os.environ.get("EMAIL_PASS")

KOOFR_WEBDAV_URL = os.environ.get("KOOFR_WEBDAV_URL", "https://app.koofr.net/dav/Koofr/")
KOOFR_USER = os.environ.get("KOOFR_USER")
KOOFR_PASS = os.environ.get("KOOFR_PASS")
KOOFR_REMOTE_FOLDER = os.environ.get("KOOFR_REMOTE_FOLDER", "/Downloads")

def get_header_str(header_value):
    if not header_value:
        return ""
    decoded = decode_header(header_value)
    result = ""
    for text, encoding in decoded:
        if isinstance(text, bytes):
            result += text.decode(encoding or "utf-8", errors="ignore")
        else:
            result += str(text)
    return result

def extract_links_and_names(text):
    """
    Ищет в тексте конструкцию: URL пробел Желаемое_Имя
    """
    pattern = r'(https?://[^\s]+)\s+([^\s]+)'
    return re.findall(pattern, text)

def process_emails():
    logs = []
    
    # 1. Подключение к IMAP
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER)
        mail.login(EMAIL_USER, EMAIL_PASS)
        mail.select("INBOX")
    except Exception as e:
        return [f" Ошибка подключения к почте: {e}"]

    # Ищем все непрочитанные письма
    status, response = mail.search(None, 'UNSEEN')
    msg_ids = response[0].split()

    if not msg_ids:
        mail.logout()
        return ["Непрочитанных писем не найдено."]

    # 2. Подключение к Koofr WebDAV
    try:
        dav = Client(
            base_url=KOOFR_WEBDAV_URL,
            auth=(KOOFR_USER, KOOFR_PASS)
        )
        if not dav.exists(KOOFR_REMOTE_FOLDER):
            dav.mkdir(KOOFR_REMOTE_FOLDER)
    except Exception as e:
        mail.logout()
        return [f" Ошибка подключения к Koofr WebDAV: {e}"]

    # 3. Обработка писем
    for msg_id in msg_ids:
        res, msg_data = mail.fetch(msg_id, '(RFC822)')
        for response_part in msg_data:
            if isinstance(response_part, tuple):
                msg = email.message_from_bytes(response_part[1])
                
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body += part.get_payload(decode=True).decode('utf-8', errors='ignore')
                else:
                    body = msg.get_payload(decode=True).decode('utf-8', errors='ignore')

                items = extract_links_and_names(body)
                if not items:
                    logs.append(f"Письмо ID {msg_id.decode()}: Подходящих ссылок и имён не найдено.")
                    continue

                for url, custom_name in items:
                    logs.append(f"Найдено: URL={url}, Имя={custom_name}")
                    
                    # Извлекаем расширение файла из URL
                    url_clean = url.split('?')[0].split('#')[0]
                    ext = os.path.splitext(url_clean)[1]
                    if not ext:
                        ext = ".bin" # Расширение по умолчанию, если в URL его нет
                    
                    filename = f"{custom_name}{ext}"
                    local_path = os.path.join("/tmp", filename)

                    # Скачивание файла на диск сервера
                    try:
                        logs.append(f"Загрузка файла на диск сервера: {filename}...")
                        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                        with urllib.request.urlopen(req) as resp, open(local_path, 'wb') as out_file:
                            out_file.write(resp.read())
                    except Exception as download_err:
                        logs.append(f" Ошибка скачивания {url}: {download_err}")
                        continue

                    # Выгрузка на Koofr
                    remote_path = os.path.join(KOOFR_REMOTE_FOLDER, filename).replace("\\", "/")
                    try:
                        logs.append(f"Загрузка на Koofr: {remote_path}...")
                        dav.upload_file(from_path=local_path, to_path=remote_path, overwrite=True)
                        logs.append(f" Успешно загружено на Koofr!")
                    except Exception as upload_err:
                        logs.append(f" Ошибка выгрузки на Koofr: {upload_err}")
                    finally:
                        # Удаление файла с локального диска сервера после попытки загрузки
                        if os.path.exists(local_path):
                            os.remove(local_path)
                            logs.append(f" Локальный файл {filename} удален с сервера.")

        # Помечаем письмо как прочитанное
        mail.store(msg_id, '+FLAGS', '\\Seen')

    mail.logout()
    return logs

@app.route("/")
@app.route("/run")
def run_task():
    results = process_emails()
    return jsonify({"status": "completed", "logs": results})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
