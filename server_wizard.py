import secrets
import os
import time
import ssl
import uuid
import sqlite3
import urllib.request
import urllib.error
from urllib.parse import urlparse
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_CANDIDATES = [
    os.getenv("DB_PATH", ""),
    os.path.join(BASE_DIR, "bot_data.db"),
    os.path.join(BASE_DIR, "bot.db"),
    "/opt/Bot_VIP/bot_data.db",
    "/opt/Bot_VIP/bot.db",
    "/opt/Bot_VIP/database.db",
    "/opt/Bot_VIP/vpn.db",
    "bot_data.db",
    "bot.db",
]

def get_db_path() -> str:
    return next((f for f in DB_CANDIDATES if f and os.path.exists(f)), os.path.join(BASE_DIR, "bot_data.db"))

def ensure_tables():
    db_file = get_db_path()
    conn = sqlite3.connect(db_file)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS servers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        host TEXT,
        ip_port TEXT,
        panel_url TEXT,
        login TEXT,
        password TEXT,
        inbound_id INTEGER DEFAULT 0,
        sub_url TEXT DEFAULT '',
        is_active INTEGER DEFAULT 1,
        created_at INTEGER DEFAULT 0
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS keys (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        server_id INTEGER,
        key TEXT,
        is_trial INTEGER DEFAULT 0,
        created_at INTEGER DEFAULT 0,
        expires_at INTEGER DEFAULT 0
    )
    """)
    conn.commit()
    conn.close()

def is_valid_panel_url(url_str: str) -> tuple[bool, str]:
    raw = url_str.strip()
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    try:
        parsed = urlparse(raw)
        if not parsed.hostname or not parsed.port:
            return False, ""
        return True, raw
    except Exception:
        return False, ""

def extract_host_port(url_str: str) -> str:
    raw = url_str.strip()
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    parsed = urlparse(raw)
    return f"{parsed.hostname}:{parsed.port}"

def test_panel_connection(panel_url: str, mode: str, token_or_login: str, password: str = "") -> tuple[bool, str, int]:
    start_t = time.time()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    norm_url = panel_url.rstrip("/")
    probe_url = f"{norm_url}/"
    headers = {
        "User-Agent": "BotVIP-Validator/1.0",
        "Accept": "application/json, text/html"
    }

    if mode == "api":
        headers["Authorization"] = token_or_login
        headers["x-api-key"] = token_or_login

    try:
        req = urllib.request.Request(probe_url, headers=headers)
        with urllib.request.urlopen(req, context=ctx, timeout=4.5) as resp:
            latency_ms = int((time.time() - start_t) * 1000)
            return True, f"Панель отвечает (HTTP {resp.status})", latency_ms
    except urllib.error.HTTPError as e:
        latency_ms = int((time.time() - start_t) * 1000)
        if e.code in (401, 403):
            return False, f"Ошибка авторизации (HTTP {e.code}: Неверный токен или пароль)", latency_ms
        if e.code == 404:
            return True, "Веб-порт открыт (HTTP 404)", latency_ms
        return True, f"Панель отвечает (HTTP {e.code})", latency_ms
    except urllib.error.URLError as e:
        reason = str(e.reason).lower()
        if "timed out" in reason:
            return False, "Таймаут подключения (хост не ответил за 4.5 сек)", 0
        if "refused" in reason:
            return False, "Отказ в подключении (порт закрыт или служба 3X-UI остановлена)", 0
        return False, f"Сетевая ошибка: {e.reason}", 0
    except Exception as e:
        return False, f"Сбой проверки: {str(e)}", 0

def test_wpp_connection_sync(wpp_url: str, token: str) -> tuple[bool, str, int]:
    import time
    import urllib.request
    import urllib.error
    import ssl

    clean_url = (wpp_url or "").strip().rstrip("/")
    if not clean_url:
        return False, "URL панели WPP не указан", 0
    if not (clean_url.startswith("http://") or clean_url.startswith("https://")):
        clean_url = f"http://{clean_url}"

    raw_token = (token or "").strip()
    auth_header = raw_token if raw_token.lower().startswith("bearer ") else f"Bearer {raw_token}"
    clean_token = raw_token[7:].strip() if raw_token.lower().startswith("bearer ") else raw_token

    test_endpoints = [
        f"{clean_url}/wpp-api/v1/profiles",
        f"{clean_url}/wpp-api/v1/status",
        f"{clean_url}/profiles",
    ]

    ctx = ssl._create_unverified_context()
    t0 = time.time()
    last_err = ""

    for ep in test_endpoints:
        try:
            req = urllib.request.Request(
                ep,
                headers={
                    "Authorization": auth_header,
                    "X-API-Key": clean_token,
                    "Accept": "application/json",
                    "User-Agent": "Bot_VIP/2.0"
                }
            )
            with urllib.request.urlopen(req, timeout=6, context=ctx) as response:
                lat = int((time.time() - t0) * 1000)
                if response.status in (200, 201, 204):
                    return True, f"WPP онлайн (HTTP {response.status})", lat
                return True, f"HTTP {response.status}", lat
        except urllib.error.HTTPError as e:
            lat = int((time.time() - t0) * 1000)
            if e.code == 401:
                return False, "401 Unauthorized (неверный токен WPP)", lat
            if e.code in (404, 405):
                last_err = f"HTTP {e.code}"
                continue
            return False, f"HTTP {e.code}: {e.reason}", lat
        except Exception as ex:
            last_err = str(ex)

    try:
        req = urllib.request.Request(clean_url, headers={"User-Agent": "Bot_VIP/2.0"})
        with urllib.request.urlopen(req, timeout=4, context=ctx) as response:
            lat = int((time.time() - t0) * 1000)
            if response.status < 500:
                return True, "Веб-порт WPP доступен", lat
    except Exception:
        pass

    lat = int((time.time() - t0) * 1000)
    return False, f"Не удалось подключиться к WPP: {last_err or 'таймаут/ошибка сети'}", lat


def save_server_record(
    name: str,
    host: str,
    panel_url: str = "",
    login: str = "",
    password: str = "",
    inbound_id: int = 0,
    sub_url: str = "",
    panel_type: str = "3xui",
    wpp_url: str = "",
    wpp_token: str = "",
    group_name: str = ""
) -> bool:
    try:
        ensure_tables()
        db_file = get_db_path()
        conn = sqlite3.connect(db_file, timeout=30.0)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(servers)")
        cols = [c[1] for c in cur.fetchall()]
        if cols:
            vals = {}
            if "name" in cols: vals["name"] = name
            if "host" in cols: vals["host"] = host
            if "ip_port" in cols: vals["ip_port"] = host
            if "panel_url" in cols: vals["panel_url"] = panel_url
            if "panel_user" in cols: vals["panel_user"] = login
            if "login" in cols: vals["login"] = login
            if "panel_pass" in cols: vals["panel_pass"] = password
            if "password" in cols: vals["password"] = password
            if "inbound_id" in cols: vals["inbound_id"] = inbound_id
            if "sub_url_prefix" in cols: vals["sub_url_prefix"] = sub_url
            if "sub_url" in cols: vals["sub_url"] = sub_url
            if "panel_type" in cols: vals["panel_type"] = panel_type
            if "wpp_url" in cols: vals["wpp_url"] = wpp_url
            if "wpp_token" in cols: vals["wpp_token"] = wpp_token
            if "group_name" in cols: vals["group_name"] = group_name
            if "is_active" in cols: vals["is_active"] = 1
            fields = ", ".join(vals.keys())
            placeholders = ", ".join(["?"] * len(vals))
            cur.execute(f"INSERT INTO servers ({fields}) VALUES ({placeholders})", list(vals.values()))
            conn.commit()
            last_id = cur.lastrowid
        conn.close()
        return last_id or True
    except Exception as e:
        print(f"Ошибка сохранения: {e}")
        return False


def render_method_choice() -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "🔐 <b>Подключение сервера</b>\n\n"
        "Выберите тип и способ подключения:\n\n"
        "🔑 <b>3X-UI (API-ключ)</b> — рекомендуется для 3X-UI 3.3.0+ (работает с 2FA).\n"
        "👤 <b>3X-UI (Логин и пароль)</b> — стандартная веб-авторизация 3X-UI.\n"
        "⚡ <b>WPP (Web-Panel-Proxy)</b> — прямое подключение к WPP-ноде.\n"
        "🟣 <b>Гибрид (3X-UI + WPP)</b> — комбинированный режим обеих панелей в одной подписке.\n\n"
        "<i>Бот выполнит проверку связи. Недоступный сервер добавлен не будет.</i>"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔑 3X-UI (API)", callback_data="admin_srvwiz_api"),
            InlineKeyboardButton("👤 3X-UI (Пароль)", callback_data="admin_srvwiz_login"),
        ],
        [
            InlineKeyboardButton("⚡ WPP (Web-Panel-Proxy)", callback_data="admin_srvwiz_wpp"),
            InlineKeyboardButton("🟣 Гибрид (3X-UI + WPP)", callback_data="admin_srvwiz_both"),
        ],
        [
            InlineKeyboardButton("⬅️ К списку серверов", callback_data="admin_servers_menu"),
            InlineKeyboardButton("🈴 На главную", callback_data="back_to_main"),
        ]
    ])
    return text, kb


def render_wizard_step(wiz: dict, error_msg: str = "") -> tuple[str, InlineKeyboardMarkup]:
    mode = wiz.get("mode", "api")
    if mode == "api":
        total = 4
    elif mode == "login":
        total = 5
    elif mode == "wpp":
        total = 4
    elif mode == "both":
        total = 6
    else:
        total = 4

    step = wiz.get("step", 1)
    err_block = f"{error_msg}\n\n" if error_msg else ""
    name_val = wiz.get("name", "")
    url_val = wiz.get("url", "")
    wpp_url_val = wiz.get("wpp_url", "")
    grp_val = wiz.get("group_label", "Все inbound’ы/тарифы")
    login_val = wiz.get("login", "")

    if step == 1:
        text = (
            f"📝 <b>Добавление сервера (Шаг {step}/{total})</b>\n\n"
            f"{err_block}"
            "Введите <b>название сервера</b>:\n"
            "<i>(например: Нидерланды-1, DE-Fast, WPP-Франция)</i>"
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="admin_srvwiz_cancel")]])
        return text, kb

    # --- Mode: 3X-UI API ---
    if mode == "api":
        if step == 2:
            text = (
                f"📝 <b>Добавление сервера 3X-UI (Шаг {step}/{total})</b>\n\n"
                f"{err_block}"
                f"✅ Название: <code>{name_val}</code>\n\n"
                "Введите <b>URL панели с портом</b>:\n"
                "<i>(например: <code>https://123.45.67.89:2053/secretpath/</code> или <code>123.45.67.89:2053</code>)</i>"
            )
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Назад", callback_data="admin_srvwiz_back"),
                InlineKeyboardButton("❌ Отмена", callback_data="admin_srvwiz_cancel"),
            ]])
            return text, kb
        if step == 3:
            text = (
                f"🧩 <b>Виртуальная группа inbound’ов (Шаг {step}/{total})</b>\n\n"
                f"{err_block}"
                f"✅ Название: <code>{name_val}</code>\n"
                f"✅ URL: <code>{url_val}</code>\n\n"
                "Номер виртуальной группы (если в тегах inbound’ов есть <code>--1</code>, <code>--2</code>):\n"
                "Отправьте цифру или нажмите <b>«⏭ Пропустить»</b>."
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("⏭ Пропустить", callback_data="admin_srvwiz_skip")],
                [
                    InlineKeyboardButton("⬅️ Назад", callback_data="admin_srvwiz_back"),
                    InlineKeyboardButton("❌ Отмена", callback_data="admin_srvwiz_cancel"),
                ]
            ])
            return text, kb
        if step == 4:
            text = (
                f"📝 <b>Добавление сервера 3X-UI (Шаг {step}/{total})</b>\n\n"
                f"{err_block}"
                f"✅ Название: <code>{name_val}</code>\n"
                f"✅ URL: <code>{url_val}</code>\n"
                f"✅ Группа: <code>{grp_val}</code>\n\n"
                "Отправьте <b>API-токен</b> панели 3X-UI:"
            )
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Назад", callback_data="admin_srvwiz_back"),
                InlineKeyboardButton("❌ Отмена", callback_data="admin_srvwiz_cancel"),
            ]])
            return text, kb

    # --- Mode: 3X-UI Login ---
    if mode == "login":
        if step == 2:
            text = (
                f"📝 <b>Добавление сервера 3X-UI (Шаг {step}/{total})</b>\n\n"
                f"{err_block}"
                f"✅ Название: <code>{name_val}</code>\n\n"
                "Введите <b>URL панели с портом</b>:\n"
                "<i>(например: <code>https://123.45.67.89:2053/secretpath/</code>)</i>"
            )
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Назад", callback_data="admin_srvwiz_back"),
                InlineKeyboardButton("❌ Отмена", callback_data="admin_srvwiz_cancel"),
            ]])
            return text, kb
        if step == 3:
            text = (
                f"🧩 <b>Виртуальная группа inbound’ов (Шаг {step}/{total})</b>\n\n"
                f"{err_block}"
                f"✅ Название: <code>{name_val}</code>\n"
                f"✅ URL: <code>{url_val}</code>\n\n"
                "Номер виртуальной группы (если в тегах есть <code>--1</code>, <code>--2</code>):\n"
                "Отправьте цифру или нажмите <b>«⏭ Пропустить»</b>."
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("⏭ Пропустить", callback_data="admin_srvwiz_skip")],
                [
                    InlineKeyboardButton("⬅️ Назад", callback_data="admin_srvwiz_back"),
                    InlineKeyboardButton("❌ Отмена", callback_data="admin_srvwiz_cancel"),
                ]
            ])
            return text, kb
        if step == 4:
            text = (
                f"📝 <b>Добавление сервера 3X-UI (Шаг {step}/{total})</b>\n\n"
                f"{err_block}"
                f"✅ Название: <code>{name_val}</code>\n"
                f"✅ URL: <code>{url_val}</code>\n"
                f"✅ Группа: <code>{grp_val}</code>\n\n"
                "Введите <b>логин</b> администратора:"
            )
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Назад", callback_data="admin_srvwiz_back"),
                InlineKeyboardButton("❌ Отмена", callback_data="admin_srvwiz_cancel"),
            ]])
            return text, kb
        if step == 5:
            text = (
                f"📝 <b>Добавление сервера 3X-UI (Шаг {step}/{total})</b>\n\n"
                f"{err_block}"
                f"✅ Название: <code>{name_val}</code>\n"
                f"✅ URL: <code>{url_val}</code>\n"
                f"✅ Группа: <code>{grp_val}</code>\n"
                f"✅ Логин: <code>{login_val}</code>\n\n"
                "Введите <b>пароль</b> администратора:"
            )
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Назад", callback_data="admin_srvwiz_back"),
                InlineKeyboardButton("❌ Отмена", callback_data="admin_srvwiz_cancel"),
            ]])
            return text, kb

    # --- Mode: WPP ---
    if mode == "wpp":
        if step == 2:
            text = (
                f"⚡ <b>Добавление WPP-сервера (Шаг {step}/{total})</b>\n\n"
                f"{err_block}"
                f"✅ Название: <code>{name_val}</code>\n\n"
                "Введите <b>URL панели WPP с портом</b>:\n"
                "<i>(например: <code>http://123.45.67.89:8000</code> или <code>https://wpp.domain.com</code>)</i>"
            )
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Назад", callback_data="admin_srvwiz_back"),
                InlineKeyboardButton("❌ Отмена", callback_data="admin_srvwiz_cancel"),
            ]])
            return text, kb
        if step == 3:
            text = (
                f"⚡ <b>Добавление WPP-сервера (Шаг {step}/{total})</b>\n\n"
                f"{err_block}"
                f"✅ Название: <code>{name_val}</code>\n"
                f"✅ URL WPP: <code>{wpp_url_val}</code>\n\n"
                "Отправьте <b>API-токен WPP</b> (Bearer token):\n"
                "<i>(он настроен в переменной WPP_API_KEY панели WPP)</i>"
            )
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Назад", callback_data="admin_srvwiz_back"),
                InlineKeyboardButton("❌ Отмена", callback_data="admin_srvwiz_cancel"),
            ]])
            return text, kb
        if step == 4:
            text = (
                f"⚡ <b>Группа тарифов для WPP (Шаг {step}/{total})</b>\n\n"
                f"{err_block}"
                f"✅ Название: <code>{name_val}</code>\n"
                f"✅ URL WPP: <code>{wpp_url_val}</code>\n\n"
                "Укажите название группы тарифов (например: <code>VIP</code> или <code>Стандарт</code>):\n"
                "<i>Сервер будет выдаваться только пользователям с тарифом этой группы.\n"
                "Нажмите <b>«⏭ Пропустить»</b>, чтобы сервер был доступен для всех тарифов.</i>"
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("⏭ Пропустить (для всех)", callback_data="admin_srvwiz_skip")],
                [
                    InlineKeyboardButton("⬅️ Назад", callback_data="admin_srvwiz_back"),
                    InlineKeyboardButton("❌ Отмена", callback_data="admin_srvwiz_cancel"),
                ]
            ])
            return text, kb

    # --- Mode: Hybrid (both) ---
    if mode == "both":
        if step == 2:
            text = (
                f"🟣 <b>Гибридный сервер: 3X-UI (Шаг {step}/{total})</b>\n\n"
                f"{err_block}"
                f"✅ Название: <code>{name_val}</code>\n\n"
                "Введите <b>URL панели 3X-UI</b> с портом:\n"
                "<i>(например: <code>https://123.45.67.89:2053/secret/</code>)</i>"
            )
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Назад", callback_data="admin_srvwiz_back"),
                InlineKeyboardButton("❌ Отмена", callback_data="admin_srvwiz_cancel"),
            ]])
            return text, kb
        if step == 3:
            text = (
                f"🟣 <b>Гибридный сервер: 3X-UI токен (Шаг {step}/{total})</b>\n\n"
                f"{err_block}"
                f"✅ Название: <code>{name_val}</code>\n"
                f"✅ URL 3X-UI: <code>{url_val}</code>\n\n"
                "Отправьте <b>API-токен</b> панели 3X-UI (или логин:пароль):"
            )
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Назад", callback_data="admin_srvwiz_back"),
                InlineKeyboardButton("❌ Отмена", callback_data="admin_srvwiz_cancel"),
            ]])
            return text, kb
        if step == 4:
            text = (
                f"🟣 <b>Гибридный сервер: WPP URL (Шаг {step}/{total})</b>\n\n"
                f"{err_block}"
                f"✅ Название: <code>{name_val}</code>\n"
                f"✅ URL 3X-UI: <code>{url_val}</code>\n\n"
                "Введите <b>URL панели WPP</b> с портом:\n"
                "<i>(например: <code>http://123.45.67.89:8000</code>)</i>"
            )
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Назад", callback_data="admin_srvwiz_back"),
                InlineKeyboardButton("❌ Отмена", callback_data="admin_srvwiz_cancel"),
            ]])
            return text, kb
        if step == 5:
            text = (
                f"🟣 <b>Гибридный сервер: WPP токен (Шаг {step}/{total})</b>\n\n"
                f"{err_block}"
                f"✅ Название: <code>{name_val}</code>\n"
                f"✅ URL 3X-UI: <code>{url_val}</code>\n"
                f"✅ URL WPP: <code>{wpp_url_val}</code>\n\n"
                "Отправьте <b>API-токен WPP</b> (Bearer token):"
            )
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Назад", callback_data="admin_srvwiz_back"),
                InlineKeyboardButton("❌ Отмена", callback_data="admin_srvwiz_cancel"),
            ]])
            return text, kb
        if step == 6:
            text = (
                f"🟣 <b>Гибридный сервер: Группа тарифов (Шаг {step}/{total})</b>\n\n"
                f"{err_block}"
                f"✅ Название: <code>{name_val}</code>\n"
                f"✅ URL 3X-UI: <code>{url_val}</code>\n"
                f"✅ URL WPP: <code>{wpp_url_val}</code>\n\n"
                "Укажите название группы тарифов (например: <code>VIP</code> или <code>Стандарт</code>):\n"
                "Нажмите <b>«⏭ Пропустить»</b>, чтобы сервер был доступен для всех тарифов."
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("⏭ Пропустить (для всех)", callback_data="admin_srvwiz_skip")],
                [
                    InlineKeyboardButton("⬅️ Назад", callback_data="admin_srvwiz_back"),
                    InlineKeyboardButton("❌ Отмена", callback_data="admin_srvwiz_cancel"),
                ]
            ])
            return text, kb

    return render_method_choice()


async def finalize_server_addition(update, context, wiz: dict, force: bool = False):
    import asyncio
    mode = wiz.get("mode", "api")

    status_msg = await update.effective_message.reply_text("⏳ <i>Проверяю связь с сервером и авторизацию...</i>", parse_mode="HTML")

    if mode == "wpp":
        wpp_url = wiz.get("wpp_url", "")
        wpp_token = wiz.get("wpp_token", "")
        ok, detail, lat = await asyncio.to_thread(test_wpp_connection_sync, wpp_url, wpp_token)
        try:
            await status_msg.delete()
        except Exception:
            pass

        if not ok:
            context.user_data["srv_wiz_draft"] = wiz
            fail_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Повторить проверку", callback_data="admin_srvwiz_retry")],
                [
                    InlineKeyboardButton("⬅️ Назад", callback_data="admin_srvwiz_back"),
                    InlineKeyboardButton("❌ Отмена", callback_data="admin_srvwiz_cancel")
                ]
            ])
            await update.effective_message.reply_text(
                f"❌ <b>WPP-сервер не прошел проверку связи!</b>\n\n"
                f"🖥 Сервер: <b>{wiz.get('name')}</b>\n"
                f"🔗 WPP URL: <code>{wpp_url}</code>\n"
                f"⚠️ <b>Причина:</b> {detail}\n\n"
                f"🚫 <b>Сервер НЕ добавлен в базу данных.</b>\n"
                f"<i>Проверьте WPP_API_KEY, порт 8000 и сетевую доступность, затем повторите.</i>",
                reply_markup=fail_kb,
                parse_mode="HTML"
            )
            return

        host_port = extract_host_port(wpp_url)
        sid = save_server_record(
            name=wiz["name"],
            host=host_port,
            panel_url="",
            login="",
            password="",
            panel_type="wpp",
            wpp_url=wpp_url,
            wpp_token=wpp_token,
            group_name=wiz.get("group_name", "")
        )
        try:
            from panel_manager import MultiPanelManager, sync_active_keys_with_servers
            asyncio.create_task(sync_active_keys_with_servers())
            if isinstance(sid, int) and sid > 0:
                asyncio.create_task(asyncio.to_thread(MultiPanelManager.scan_server_protocols, sid, get_db_path()))
                asyncio.create_task(MultiPanelManager.auto_provision_server_to_group_users(sid, get_db_path()))
        except Exception:
            pass
        context.user_data.pop("srv_wiz", None)
        context.user_data.pop("srv_wiz_draft", None)
        fin_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🖥 К списку серверов", callback_data="admin_servers_menu")],
            [InlineKeyboardButton("🈴 На главную", callback_data="back_to_main")]
        ])
        await update.effective_message.reply_text(
            f"✅ <b>WPP-сервер успешно проверен и добавлен!</b>\n\n"
            f"🖥 Название: <b>{wiz['name']}</b>\n"
            f"🔗 Панель: <code>{wpp_url}</code>\n"
            f"🟢 <b>Статус:</b> В сети ({lat} ms)\n"
            f"🏷 Группа тарифов: <code>{wiz.get('group_name') or 'Все тарифы'}</code>",
            reply_markup=fin_kb,
            parse_mode="HTML"
        )
        return

    elif mode == "both":
        url_3x = wiz.get("url", "")
        tok_3x = wiz.get("api_token", "")
        wpp_url = wiz.get("wpp_url", "")
        wpp_token = wiz.get("wpp_token", "")

        ok_3x, det_3x, lat_3x = await asyncio.to_thread(test_panel_connection, url_3x, "api", tok_3x)
        ok_wpp, det_wpp, lat_wpp = await asyncio.to_thread(test_wpp_connection_sync, wpp_url, wpp_token)
        try:
            await status_msg.delete()
        except Exception:
            pass

        if not ok_3x or not ok_wpp:
            context.user_data["srv_wiz_draft"] = wiz
            fail_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Повторить проверку", callback_data="admin_srvwiz_retry")],
                [
                    InlineKeyboardButton("⬅️ Назад", callback_data="admin_srvwiz_back"),
                    InlineKeyboardButton("❌ Отмена", callback_data="admin_srvwiz_cancel")
                ]
            ])
            err_3x_msg = f"✅ 3X-UI: Онлайн ({lat_3x} ms)" if ok_3x else f"❌ 3X-UI: {det_3x}"
            err_wpp_msg = f"✅ WPP: Онлайн ({lat_wpp} ms)" if ok_wpp else f"❌ WPP: {det_wpp}"
            await update.effective_message.reply_text(
                f"❌ <b>Один или оба сервиса гибридного сервера недоступны!</b>\n\n"
                f"🖥 Сервер: <b>{wiz.get('name')}</b>\n"
                f"{err_3x_msg}\n"
                f"{err_wpp_msg}\n\n"
                f"🚫 <b>Сервер НЕ добавлен в базу данных.</b>",
                reply_markup=fail_kb,
                parse_mode="HTML"
            )
            return

        host_port = extract_host_port(url_3x)
        sid = save_server_record(
            name=wiz["name"],
            host=host_port,
            panel_url=url_3x,
            login="api_token",
            password=tok_3x,
            panel_type="both",
            wpp_url=wpp_url,
            wpp_token=wpp_token,
            group_name=wiz.get("group_name", "")
        )
        try:
            from panel_manager import MultiPanelManager, sync_active_keys_with_servers
            asyncio.create_task(sync_active_keys_with_servers())
            if isinstance(sid, int) and sid > 0:
                asyncio.create_task(asyncio.to_thread(MultiPanelManager.scan_server_protocols, sid, get_db_path()))
                asyncio.create_task(MultiPanelManager.auto_provision_server_to_group_users(sid, get_db_path()))
        except Exception:
            pass
        context.user_data.pop("srv_wiz", None)
        context.user_data.pop("srv_wiz_draft", None)
        fin_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🖥 К списку серверов", callback_data="admin_servers_menu")],
            [InlineKeyboardButton("🈴 На главную", callback_data="back_to_main")]
        ])
        await update.effective_message.reply_text(
            f"✅ <b>Гибридный сервер (3X-UI + WPP) успешно добавлен!</b>\n\n"
            f"🖥 Название: <b>{wiz['name']}</b>\n"
            f"🔗 3X-UI: <code>{url_3x}</code> ({lat_3x} ms)\n"
            f"⚡ WPP: <code>{wpp_url}</code> ({lat_wpp} ms)\n"
            f"🏷 Группа тарифов: <code>{wiz.get('group_name') or 'Все тарифы'}</code>",
            reply_markup=fin_kb,
            parse_mode="HTML"
        )
        return

    else:
        url = wiz.get("url", "")
        token_or_login = wiz.get("api_token" if mode == "api" else "login", "")
        pwd = wiz.get("password", "")

        ok, detail, lat = await asyncio.to_thread(test_panel_connection, url, mode, token_or_login, pwd)
        try:
            await status_msg.delete()
        except Exception:
            pass

        if not ok:
            context.user_data["srv_wiz_draft"] = wiz
            fail_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Повторить проверку", callback_data="admin_srvwiz_retry")],
                [
                    InlineKeyboardButton("⬅️ Назад", callback_data="admin_srvwiz_back"),
                    InlineKeyboardButton("❌ Отмена", callback_data="admin_srvwiz_cancel")
                ]
            ])
            await update.effective_message.reply_text(
                f"❌ <b>Сервер не прошел проверку связи!</b>\n\n"
                f"🖥 Сервер: <b>{wiz['name']}</b>\n"
                f"🔗 Панель: <code>{wiz['url']}</code>\n"
                f"⚠️ <b>Причина:</b> {detail}\n\n"
                f"🚫 <b>Сервер НЕ добавлен в базу данных.</b>\n"
                f"<i>Исправьте адрес панели, проверьте порт и фаервол, затем повторите проверку.</i>",
                reply_markup=fail_kb,
                parse_mode="HTML"
            )
            return

        host_port = extract_host_port(url)
        sid = save_server_record(
            name=wiz["name"],
            host=host_port,
            panel_url=url,
            login="api_token" if mode == "api" else wiz.get("login", ""),
            password=pwd or token_or_login,
            inbound_id=wiz.get("group_id", 0),
            panel_type="3xui",
            group_name=wiz.get("group_name", "")
        )
        try:
            from panel_manager import MultiPanelManager, sync_active_keys_with_servers
            asyncio.create_task(sync_active_keys_with_servers())
            if isinstance(sid, int) and sid > 0:
                asyncio.create_task(asyncio.to_thread(MultiPanelManager.scan_server_protocols, sid, get_db_path()))
                asyncio.create_task(MultiPanelManager.auto_provision_server_to_group_users(sid, get_db_path()))
        except Exception:
            pass
        context.user_data.pop("srv_wiz", None)
        context.user_data.pop("srv_wiz_draft", None)
        fin_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🖥 К списку серверов", callback_data="admin_servers_menu")],
            [InlineKeyboardButton("🈴 На главную", callback_data="back_to_main")]
        ])
        await update.effective_message.reply_text(
            f"✅ <b>Сервер успешно проверен и добавлен!</b>\n\n"
            f"🖥 Название: <b>{wiz['name']}</b>\n"
            f"🔗 Панель: <code>{wiz['url']}</code>\n"
            f"🟢 <b>Статус:</b> В сети ({lat} ms)\n"
            f"🧩 Группа inbound’ов: <code>{wiz.get('group_label', 'Все inbound’ы')}</code>",
            reply_markup=fin_kb,
            parse_mode="HTML"
        )


async def handle_wizard_input(update, context) -> bool:
    if not context.user_data.get("srv_wiz"):
        return False

    raw_val = update.message.text.strip() if (update.message and update.message.text) else ""
    menu_markers = ("👤", "🌐", "🔑", "⚙️", "📢", "🆘", "🤖", "🎟", "🔗", "💳", "🎁", "📊", "👥", "🖥", "/")
    if any(raw_val.startswith(m) for m in menu_markers):
        context.user_data.pop("srv_wiz", None)
        context.user_data.pop("awaiting", None)
        return False

    wiz = context.user_data["srv_wiz"]
    step = wiz.get("step", 1)
    mode = wiz.get("mode", "api")

    # Common Step 1: Server Name
    if step == 1:
        wiz["name"] = raw_val
        wiz["step"] = 2
        w_text, w_kb = render_wizard_step(wiz)
        await update.message.reply_text(w_text, reply_markup=w_kb, parse_mode="HTML")
        return True

    # --- Mode: 3X-UI API ---
    if mode == "api":
        if step == 2:
            ok, norm_url = is_valid_panel_url(raw_val)
            if not ok:
                err = "❌ Введите корректный адрес с портом, например: <code>https://123.45.67.89:2053/api/</code>"
                w_text, w_kb = render_wizard_step(wiz, error_msg=err)
                await update.message.reply_text(w_text, reply_markup=w_kb, parse_mode="HTML")
                return True
            wiz["url"] = norm_url
            wiz["step"] = 3
            w_text, w_kb = render_wizard_step(wiz)
            await update.message.reply_text(w_text, reply_markup=w_kb, parse_mode="HTML")
            return True
        if step == 3:
            cleaned = raw_val.replace("--", "").strip()
            if not cleaned.isdigit():
                err = "❌ Введите номер группы цифрой (например: <code>1</code>) или нажмите кнопку «⏭ Пропустить»."
                w_text, w_kb = render_wizard_step(wiz, error_msg=err)
                await update.message.reply_text(w_text, reply_markup=w_kb, parse_mode="HTML")
                return True
            wiz["group_id"] = int(cleaned)
            wiz["group_label"] = f"--{cleaned}"
            wiz["step"] = 4
            w_text, w_kb = render_wizard_step(wiz)
            await update.message.reply_text(w_text, reply_markup=w_kb, parse_mode="HTML")
            return True
        if step == 4:
            wiz["api_token"] = raw_val
            await finalize_server_addition(update, context, wiz)
            return True

    # --- Mode: 3X-UI Login ---
    if mode == "login":
        if step == 2:
            ok, norm_url = is_valid_panel_url(raw_val)
            if not ok:
                err = "❌ Введите корректный адрес с портом, например: <code>https://123.45.67.89:2053/secret/</code>"
                w_text, w_kb = render_wizard_step(wiz, error_msg=err)
                await update.message.reply_text(w_text, reply_markup=w_kb, parse_mode="HTML")
                return True
            wiz["url"] = norm_url
            wiz["step"] = 3
            w_text, w_kb = render_wizard_step(wiz)
            await update.message.reply_text(w_text, reply_markup=w_kb, parse_mode="HTML")
            return True
        if step == 3:
            cleaned = raw_val.replace("--", "").strip()
            if not cleaned.isdigit():
                err = "❌ Введите номер группы цифрой (например: <code>1</code>) или нажмите кнопку «⏭ Пропустить»."
                w_text, w_kb = render_wizard_step(wiz, error_msg=err)
                await update.message.reply_text(w_text, reply_markup=w_kb, parse_mode="HTML")
                return True
            wiz["group_id"] = int(cleaned)
            wiz["group_label"] = f"--{cleaned}"
            wiz["step"] = 4
            w_text, w_kb = render_wizard_step(wiz)
            await update.message.reply_text(w_text, reply_markup=w_kb, parse_mode="HTML")
            return True
        if step == 4:
            wiz["login"] = raw_val
            wiz["step"] = 5
            w_text, w_kb = render_wizard_step(wiz)
            await update.message.reply_text(w_text, reply_markup=w_kb, parse_mode="HTML")
            return True
        if step == 5:
            wiz["password"] = raw_val
            await finalize_server_addition(update, context, wiz)
            return True

    # --- Mode: WPP ---
    if mode == "wpp":
        if step == 2:
            ok, norm_url = is_valid_panel_url(raw_val)
            if not ok:
                err = "❌ Введите корректный адрес с портом, например: <code>http://123.45.67.89:8000</code>"
                w_text, w_kb = render_wizard_step(wiz, error_msg=err)
                await update.message.reply_text(w_text, reply_markup=w_kb, parse_mode="HTML")
                return True
            wiz["wpp_url"] = norm_url
            wiz["step"] = 3
            w_text, w_kb = render_wizard_step(wiz)
            await update.message.reply_text(w_text, reply_markup=w_kb, parse_mode="HTML")
            return True
        if step == 3:
            wiz["wpp_token"] = raw_val
            wiz["step"] = 4
            w_text, w_kb = render_wizard_step(wiz)
            await update.message.reply_text(w_text, reply_markup=w_kb, parse_mode="HTML")
            return True
        if step == 4:
            wiz["group_name"] = raw_val
            wiz["group_label"] = raw_val
            await finalize_server_addition(update, context, wiz)
            return True

    # --- Mode: Hybrid (both) ---
    if mode == "both":
        if step == 2:
            ok, norm_url = is_valid_panel_url(raw_val)
            if not ok:
                err = "❌ Введите корректный адрес с портом, например: <code>https://123.45.67.89:2053/secret/</code>"
                w_text, w_kb = render_wizard_step(wiz, error_msg=err)
                await update.message.reply_text(w_text, reply_markup=w_kb, parse_mode="HTML")
                return True
            wiz["url"] = norm_url
            wiz["step"] = 3
            w_text, w_kb = render_wizard_step(wiz)
            await update.message.reply_text(w_text, reply_markup=w_kb, parse_mode="HTML")
            return True
        if step == 3:
            wiz["api_token"] = raw_val
            wiz["step"] = 4
            w_text, w_kb = render_wizard_step(wiz)
            await update.message.reply_text(w_text, reply_markup=w_kb, parse_mode="HTML")
            return True
        if step == 4:
            ok, norm_url = is_valid_panel_url(raw_val)
            if not ok:
                err = "❌ Введите корректный адрес с портом, например: <code>http://123.45.67.89:8000</code>"
                w_text, w_kb = render_wizard_step(wiz, error_msg=err)
                await update.message.reply_text(w_text, reply_markup=w_kb, parse_mode="HTML")
                return True
            wiz["wpp_url"] = norm_url
            wiz["step"] = 5
            w_text, w_kb = render_wizard_step(wiz)
            await update.message.reply_text(w_text, reply_markup=w_kb, parse_mode="HTML")
            return True
        if step == 5:
            wiz["wpp_token"] = raw_val
            wiz["step"] = 6
            w_text, w_kb = render_wizard_step(wiz)
            await update.message.reply_text(w_text, reply_markup=w_kb, parse_mode="HTML")
            return True
        if step == 6:
            wiz["group_name"] = raw_val
            wiz["group_label"] = raw_val
            await finalize_server_addition(update, context, wiz)
            return True

    return False


async def _reply_or_edit(update, query, staff_roles_mod, text: str, kb=None):
    if query and staff_roles_mod and hasattr(staff_roles_mod, "safe_edit"):
        await staff_roles_mod.safe_edit(query, text, kb)
    elif query:
        try:
            await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            await update.effective_message.reply_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await update.effective_message.reply_text(text, reply_markup=kb, parse_mode="HTML")

async def issue_instant_trial(update, context, staff_roles_mod=None):
    query = update.callback_query
    user = update.effective_user

    if query:
        try:
            await query.answer()
        except Exception:
            pass

    context.user_data.pop("awaiting", None)
    context.user_data.pop("srv_wiz", None)

    ensure_tables()
    db_file = get_db_path()
    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🈴 На главную", callback_data="back_to_main")]])

    try:
        conn = sqlite3.connect(db_file)
        cur = conn.cursor()

        cur.execute("PRAGMA table_info(users)")
        u_cols = [c[1] for c in cur.fetchall()]
        uid_col = next((c for c in ("user_id", "telegram_id", "tg_id", "id") if c in u_cols), None)

        if u_cols and "trial_used" not in u_cols:
            try:
                cur.execute("ALTER TABLE users ADD COLUMN trial_used INTEGER DEFAULT 0")
                conn.commit()
                u_cols.append("trial_used")
            except Exception:
                pass

        if uid_col and "trial_used" in u_cols:
            cur.execute(f"SELECT trial_used FROM users WHERE {uid_col} = ?", (user.id,))
            row = cur.fetchone()
            if row and row[0] == 1:
                conn.close()
                msg = (
                    "⚠️ <b>Вы уже использовали пробную подписку.</b>\n\n"
                    "Для продления доступа перейдите в раздел <b>«💳 Купить подписку»</b>."
                )
                await _reply_or_edit(update, query, staff_roles_mod, msg, back_kb)
                return

        cur.execute("SELECT COUNT(*) FROM keys WHERE user_id = ? AND is_trial = 1", (user.id,))
        k_row = cur.fetchone()
        if k_row and k_row[0] > 0:
            conn.close()
            msg = (
                "⚠️ <b>Вы уже использовали пробную подписку.</b>\n\n"
                "Для продления доступа перейдите в раздел <b>«💳 Купить подписку»</b>."
            )
            await _reply_or_edit(update, query, staff_roles_mod, msg, back_kb)
            return

        trial_days = 3
        try:
            cur.execute("SELECT value FROM settings WHERE key IN ('trial_days', 'test_period_days')")
            s_row = cur.fetchone()
            if s_row and str(s_row[0]).isdigit():
                trial_days = int(s_row[0])
        except Exception:
            pass

        cur.execute("SELECT id, name, host FROM servers WHERE is_active = 1 LIMIT 1")
        srv = cur.fetchone()
        if not srv:
            cur.execute("SELECT id, name, host FROM servers LIMIT 1")
            srv = cur.fetchone()

        if not srv:
            conn.close()
            no_srv_msg = (
                "⚠️ <b>В боте пока нет активных серверов.</b>\n\n"
                "Администратор ещё не подключил сервер для генерации ключей. "
                "Пожалуйста, попробуйте позже или обратитесь в поддержку."
            )
            await _reply_or_edit(update, query, staff_roles_mod, no_srv_msg, back_kb)
            return

        srv_id, srv_name, srv_host = srv[0], srv[1], srv[2] or "server"
        key_uuid = str(uuid.uuid4())
        key_token = f"vless://{key_uuid}@{srv_host}:443?security=reality#VIP-Trial-{user.id}"
        now_ts = int(time.time())
        expire_ts = now_ts + (trial_days * 86400)

        cur.execute(
            "INSERT INTO keys (user_id, server_id, key, is_trial, created_at, expires_at) VALUES (?, ?, ?, 1, ?, ?)",
            (user.id, srv_id, key_token, now_ts, expire_ts)
        )
        if uid_col and "trial_used" in u_cols:
            cur.execute(f"UPDATE users SET trial_used = 1 WHERE {uid_col} = ?", (user.id,))

        conn.commit()
        conn.close()

        success_text = (
            f"🎁 <b>Пробная подписка успешно активирована!</b>\n\n"
            f"⏳ Срок действия: <b>{trial_days} дн.</b>\n"
            f"🖥 Локация: <b>{srv_name}</b>\n\n"
            f"🔑 <b>Ваш ключ подключения:</b>\n<code>{key_token}</code>\n\n"
            f"Скопируйте ключ и вставьте его в приложение для подключения (V2rayN, Happ, Hiddify, Streisand)."
        )
        fin_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📖 Инструкция", callback_data="help_connect")],
            [InlineKeyboardButton("🈴 На главную", callback_data="back_to_main")]
        ])
        await _reply_or_edit(update, query, staff_roles_mod, success_text, fin_kb)

    except Exception as exc:
        print(f"Ошибка в issue_instant_trial: {exc}")
        err_msg = "❌ Не удалось выдать пробный период. Обратитесь в поддержку."
        await _reply_or_edit(update, query, staff_roles_mod, err_msg, back_kb)

def get_user_sub_url(user_id: int) -> str:
    ensure_tables()
    db_file = get_db_path()
    conn = sqlite3.connect(db_file)
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(users)")
    cols = [c[1] for c in cur.fetchall()]
    uid_col = next((c for c in ("user_id", "telegram_id", "tg_id", "id") if c in cols), "user_id")

    if "sub_token" not in cols:
        try:
            cur.execute("ALTER TABLE users ADD COLUMN sub_token TEXT")
            conn.commit()
        except Exception:
            pass

    cur.execute(f"SELECT sub_token FROM users WHERE {uid_col} = ?", (user_id,))
    row = cur.fetchone()
    if row and row[0]:
        tkn = row[0]
    else:
        tkn = "sub_" + secrets.token_urlsafe(24)
        try:
            cur.execute(f"UPDATE users SET sub_token = ? WHERE {uid_col} = ?", (tkn, user_id))
            conn.commit()
        except Exception:
            pass
    conn.close()
    return f"http://177.202.68.108:2097/sub/{tkn}"

async def render_my_keys(update, context, staff_roles_mod=None):
    query = update.callback_query
    user = update.effective_user

    if query:
        try:
            await query.answer()
        except Exception:
            pass

    context.user_data.pop("awaiting", None)
    context.user_data.pop("srv_wiz", None)

    ensure_tables()
    db_file = get_db_path()
    sub_url = get_user_sub_url(user.id)

    conn = sqlite3.connect(db_file)
    cur = conn.cursor()

    now_ts = int(time.time())
    cur.execute("SELECT expires_at, is_trial FROM keys WHERE user_id = ? ORDER BY expires_at DESC LIMIT 1", (user.id,))
    k_row = cur.fetchone()

    cur.execute("SELECT COUNT(*) FROM servers WHERE is_active = 1")
    srv_cnt_row = cur.fetchone()
    srv_count = srv_cnt_row[0] if srv_cnt_row else 0
    conn.close()

    if k_row and k_row[0] and k_row[0] > now_ts:
        import datetime
        exp_dt = datetime.datetime.fromtimestamp(k_row[0]).strftime("%d.%m.%Y %H:%M")
        status_line = f"🟢 <b>Активна</b> (до <code>{exp_dt}</code>)"
    elif k_row and k_row[0]:
        status_line = "🔴 <b>Истекла</b> (требуется продление)"
    else:
        status_line = "⚪️ <b>Нет активных подписок</b> (получите триал или оформите покупку)"

    text = (
        "🔑 <b>Ваши ключи и ссылка подключения</b>\n\n"
        f"👤 Ваш ID: <code>{user.id}</code>\n"
        f"📊 Статус подписки: {status_line}\n"
        f"🖥 Активных серверов в подписке: <b>{srv_count} шт.</b>\n\n"
        "🔗 <b>Единая ссылка подписки:</b>\n"
        f"<code>{sub_url}</code>\n\n"
        "<i>(Нажмите на ссылку выше, чтобы моментально скопировать её)</i>\n\n"
        "📲 <b>Как подключить:</b>\n"
        "1. Скопируйте ссылку подписки.\n"
        "2. Откройте приложение (<b>Hiddify</b>, <b>Happ</b>, <b>Streisand</b>, <b>V2rayN</b>).\n"
        "3. Нажмите <b>«+»</b> или <b>«Добавить из буфера»</b> — все доступные серверы загрузятся автоматически."
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📖 Инструкция по приложениям", callback_data="help_connect")],
        [InlineKeyboardButton("💳 Продлить / Купить", callback_data="buy_vpn")],
        [InlineKeyboardButton("🈴 На главную", callback_data="back_to_main")]
    ])

    await _reply_or_edit(update, query, staff_roles_mod, text, kb)
