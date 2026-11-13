import os, sys, time, sqlite3, gzip, shutil, re
try:
    import requests
except ImportError:
    requests = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_DIR = BASE_DIR
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
DB_PATH = os.getenv("DB_PATH", os.path.join(BASE_DIR, "bot_data.db"))
ADMIN_ID = os.getenv("ADMIN_ID", "5167074427")
os.makedirs(BACKUP_DIR, exist_ok=True)

# 1. Поиск токена бота
token = None
for p in [os.path.join(BOT_DIR, ".env"), "/opt/bot_release/.env", os.path.join(BOT_DIR, "bot.py")]:
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8", errors="ignore") as f:
            m = re.search(r"(\d{8,11}:[A-Za-z0-9_-]{35})", f.read())
            if m:
                token = m.group(1)
                break

if not token:
    print("❌ Токен бота не найден.")
    sys.exit(1)

# 2. Горячий снимок БД SQLite (Online Backup API)
now_str = time.strftime("%Y%m%d_%H%M%S")
tmp_db = os.path.join(BACKUP_DIR, f"bot_backup_{now_str}.db")
gz_file = tmp_db + ".gz"

print(f"⏳ Создание горячего снимка базы {DB_PATH}...")
src_conn = sqlite3.connect(DB_PATH)
dst_conn = sqlite3.connect(tmp_db)
with dst_conn:
    src_conn.backup(dst_conn)
dst_conn.close()
src_conn.close()

# 3. Сжатие в gzip
print(f"⏳ Сжатие в архив {gz_file}...")
with open(tmp_db, "rb") as f_in, gzip.open(gz_file, "wb") as f_out:
    shutil.copyfileobj(f_in, f_out)
if os.path.exists(tmp_db):
    os.remove(tmp_db)

# 4. Отправка документа в Telegram админу
print(f"⏳ Отправка архива в Telegram (chat_id: {ADMIN_ID})...")
url = f"https://api.telegram.org/bot{token}/sendDocument"
with open(gz_file, "rb") as f:
    r = requests.post(
        url,
        data={"chat_id": ADMIN_ID, "caption": f"🌙 Автобэкап базы данных ({now_str})"},
        files={"document": f},
        timeout=40
    )

if r.status_code == 200:
    print("✅ Документ успешно доставлен в ваш Telegram!")
else:
    print(f"⚠️ Ошибка отправки Telegram ({r.status_code}): {r.text}")

# 5. Ротация (удаление копий старше 14 дней)
now_ts = time.time()
for fname in os.listdir(BACKUP_DIR):
    if fname.startswith("bot_backup_") and fname.endswith(".db.gz"):
        fpath = os.path.join(BACKUP_DIR, fname)
        if now_ts - os.path.getmtime(fpath) > 14 * 86400:
            try:
                os.remove(fpath)
            except Exception:
                pass

print("✅ Бэкап и ротация завершены успешно.")
