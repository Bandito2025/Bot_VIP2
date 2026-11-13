#!/usr/bin/env bash
set -e

BACKUP_DIR="/opt/backups"
SRC_DB="/opt/Bot_VIP/bot_data.db"
BOT_DIR="/opt/Bot_VIP"
ADMIN_ID="651094093"
mkdir -p "$BACKUP_DIR"

TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
TEMP_DB="${BACKUP_DIR}/bot_backup_${TIMESTAMP}.db"
GZ_DB="${TEMP_DB}.gz"

# 1. Горячий снимок и сжатие
sqlite3 "$SRC_DB" ".backup '${TEMP_DB}'"
gzip -f "$TEMP_DB"

# 2. Автоматическая отправка копии в Telegram админу
python3 - <<PYEOF
import os, sys, requests, re, sqlite3

gz_file = "${GZ_DB}"
admin_id = "${ADMIN_ID}"
db_path = "${SRC_DB}"
token = None

# Извлечение токена из БД
try:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        for r in conn.execute("SELECT * FROM settings").fetchall():
            d = dict(r)
            k = str(d.get("key", "")).lower()
            v = str(d.get("value") or d.get("val") or "").strip()
            if "token" in k and re.match(r"^\d{8,11}:[A-Za-z0-9_-]{35}$", v):
                token = v
                break
except Exception:
    pass

# Извлечение токена из .env при необходимости
if not token:
    env_p = "/opt/Bot_VIP/.env"
    if os.path.exists(env_p):
        with open(env_p, "r", encoding="utf-8") as f:
            for line in f:
                if "=" in line and ("TOKEN" in line.upper() or "BOT" in line.upper()):
                    val = line.split("=", 1)[1].strip().strip("\"'\''")
                    if re.match(r"^\d{8,11}:[A-Za-z0-9_-]{35}$", val):
                        token = val
                        break

if token and os.path.exists(gz_file):
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    with open(gz_file, "rb") as f:
        requests.post(
            url,
            data={"chat_id": admin_id, "caption": f"🌙 Автобэкап базы данных (${TIMESTAMP})"},
            files={"document": f},
            timeout=30
        )
PYEOF

# 3. Удаление локальных копий старше 14 дней
find "$BACKUP_DIR" -type f -name "bot_backup_*.db.gz" -mtime +14 -delete
