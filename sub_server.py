import os
import re
import sqlite3
import base64
import logging
import threading
import subprocess
import urllib.parse
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

logger = logging.getLogger("Bot_VIP.SubServer")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("DB_PATH", os.path.join(BASE_DIR, "bot_data.db"))

def get_db_conn(db_path: str = None):
    p = db_path or DB_PATH
    conn = sqlite3.connect(p, timeout=30.0)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn

def auto_repair_caddy() -> str:
    """Находит домен сервера в Caddyfile и гарантирует наличие reverse_proxy для /sub/*"""
    caddy_path = "/etc/caddy/Caddyfile"
    if not os.path.exists(caddy_path):
        return None
    try:
        # Автоматическое открытие портов для выпуска SSL Caddy на Linux
        try:
            subprocess.run(["ufw", "allow", "80/tcp"], capture_output=True)
            subprocess.run(["ufw", "allow", "443/tcp"], capture_output=True)
            subprocess.run(["ufw", "allow", "2097/tcp"], capture_output=True)
        except Exception:
            pass

        with open(caddy_path, "r", encoding="utf-8") as f:
            content = f.read()

        domains = re.findall(r'^\s*([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})(?::\d+)?\s*\{', content, re.M)
        domain = None
        for d in domains:
            if not d.startswith("sub."):
                domain = d
                break
        if not domain and domains:
            domain = domains[0]

        if "2097" not in content and domain:
            rule = "\n        handle /sub/* {\n                reverse_proxy 127.0.0.1:2097\n        }\n"
            if "handle /wpp-api/*" in content:
                new_content = content.replace("handle /wpp-api/*", rule.strip() + "\n\n        handle /wpp-api/*", 1)
            elif "handle_path" in content:
                pos = content.find("handle_path")
                new_content = content[:pos] + rule.strip() + "\n\n        " + content[pos:]
            else:
                pos = content.find("}")
                new_content = content[:pos] + rule + content[pos:] if pos != -1 else content + rule

            with open(caddy_path, "w", encoding="utf-8") as f:
                f.write(new_content)

            subprocess.run(["systemctl", "reload", "caddy"], capture_output=True)
            logger.info(f"✅ Caddyfile обновлен: /sub/* направлен на 2097 для {domain}")

        return domain
    except Exception as e:
        logger.error(f"Ошибка auto_repair_caddy: {e}")
        return None

def auto_heal_sub_database(db_path: str = DB_PATH):
    """Вычищает порт 2097 из настроек базы данных и переводит домен на чистый https://"""
    if not os.path.exists(db_path):
        return
    try:
        discovered_domain = auto_repair_caddy()
        with get_db_conn(db_path) as conn:
            row = conn.execute("SELECT value FROM settings WHERE key='sub_domain_prefix'").fetchone()
            current_val = row[0].strip() if (row and row[0]) else ""

            clean_val = current_val
            if ":2097" in clean_val:
                clean_val = clean_val.replace(":2097", "")
            if clean_val.startswith("http://") and any(ext in clean_val for ext in [".online", ".ru", ".com", ".net", ".org"]):
                clean_val = "https://" + clean_val[7:]

            if not clean_val or "sub.bot-vip.online" in clean_val:
                if discovered_domain:
                    clean_val = f"https://{discovered_domain}/sub"

            if clean_val and not clean_val.endswith("/sub"):
                clean_val = clean_val.rstrip("/") + "/sub"

            if clean_val != current_val and clean_val:
                conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('sub_domain_prefix', ?)", (clean_val,))
                conn.commit()
                logger.info(f"✅ Sub URL в базе исправлен: {clean_val}")
    except Exception as e:
        logger.error(f"Ошибка auto_heal_sub_database: {e}")

def normalize_sub_base_url(raw: str) -> str:
    """
    Преобразует произвольный хост/URL в стандартный формат https://HOST:8443.
    - Очищает любые дублирующиеся протоколы: https://https:// -> https://
    - Голый IPv4 (177.202.68.108)        → https://177-202-68-108.sslip.io:8443
    - Домен без схемы (sub.domain.com)   → https://sub.domain.com:8443
    - https://... без кастомного порта   → добавляет :8443
    - Не дублирует :8443:8443
    """
    if not raw:
        return raw
    cleaned = raw.strip().rstrip('/')
    cleaned = re.sub(r'^(https?://)+', '', cleaned, flags=re.IGNORECASE).strip().rstrip('/')
    if not cleaned:
        return raw

    if ':8443' in cleaned:
        res = f'https://{cleaned}'
        return re.sub(r'^(https?://)+', 'https://', res, flags=re.IGNORECASE)

    host = cleaned.split('/')[0].split(':')[0]
    if re.match(r'^\d{1,3}(\.\d{1,3}){3}$', host) and host not in ('127.0.0.1', 'localhost'):
        sslip = host.replace('.', '-') + '.sslip.io'
        return f'https://{sslip}:8443'
    if host in ('localhost', '127.0.0.1'):
        return f'http://{cleaned}' if 'http' in raw else cleaned
    return f'https://{host}:8443'


def get_clean_sub_url(sub_id: str, db_path: str = DB_PATH) -> str:
    """Формирует чистый HTTPS URL подписки с портом 8443 без дублирования протоколов"""
    clean_sub_id = str(sub_id).strip().strip('/')
    env_sub = os.getenv("SUB_BASE_URL", "").strip()
    if env_sub:
        norm_base = normalize_sub_base_url(env_sub).rstrip("/")
        full = f"{norm_base}/sub/{clean_sub_id}"
        return re.sub(r'^(https?://)+', 'https://', full, flags=re.IGNORECASE)

    prefix = ""
    try:
        if os.path.exists(db_path):
            with get_db_conn(db_path) as conn:
                row = conn.execute("SELECT value FROM settings WHERE key='sub_domain_prefix'").fetchone()
                if row and row[0]:
                    prefix = row[0].strip()
    except Exception:
        pass

    if not prefix or ":2097" in prefix or "bot-vip.online" in prefix:
        caddy_domain = auto_repair_caddy()
        if caddy_domain:
            prefix = normalize_sub_base_url(caddy_domain)
        else:
            prefix = prefix.replace(":2097", "") if prefix else "https://sub.svoii-vpn.online:8443"

    prefix = normalize_sub_base_url(prefix).rstrip("/")
    if not prefix.endswith("/sub"):
        prefix = prefix + "/sub"

    full = f"{prefix}/{clean_sub_id}"
    return re.sub(r'^(https?://)+', 'https://', full, flags=re.IGNORECASE)


def build_subscription_payload(sub_id: str, headers: dict = None, query: str = "", client_ip: str = "127.0.0.1", db_path: str = None) -> tuple[int, dict, bytes]:
    """
    Единый генератор подписки для внешнего сервера sub_server и встроенного HTTP-сервера в bot.py.
    Возвращает (status_code, response_headers, response_body_bytes).
    """
    headers = headers or {}
    p = db_path or DB_PATH
    if not os.path.exists(p):
        return 500, {"Content-Type": "text/plain; charset=utf-8"}, b"Database not found"

    # 1. Поиск ключа в базе
    k_id, user_id, client_uuid, loc_name, grp_name, exp_str, lim_gb, traffic_bytes, cfg_saved, is_active = (
        None, None, "", "", "", "", 0, 0, "", 1
    )

    with get_db_conn(p) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        k_row = cur.execute(
            "SELECT id, user_id, client_uuid, location_name, group_name, expires_at, limit_gb, traffic_bytes, config_text, is_active "
            "FROM user_keys WHERE sub_id = ?",
            (sub_id,)
        ).fetchone()

        if k_row:
            k_id = k_row["id"]
            user_id = k_row["user_id"]
            client_uuid = k_row["client_uuid"]
            loc_name = k_row["location_name"] or "🇩🇪 Германия"
            grp_name = k_row["group_name"] or ""
            exp_str = k_row["expires_at"] or ""
            lim_gb = float(k_row["limit_gb"] or 0)
            traffic_bytes = int(k_row["traffic_bytes"] or 0)
            cfg_saved = k_row["config_text"] or ""
            is_active = k_row["is_active"]

            # Агрегация трафика со всех привязанных нод из user_key_nodes
            try:
                cur.execute("SELECT SUM(up_bytes + down_bytes) FROM user_key_nodes WHERE key_id = ?", (k_id,))
                sum_row = cur.fetchone()
                if sum_row and sum_row[0] is not None and int(sum_row[0]) > traffic_bytes:
                    traffic_bytes = int(sum_row[0])
            except Exception:
                pass
        else:
            # Поиск в таблице users
            u_row = cur.execute(
                "SELECT user_id, client_uuid, vip_until, vpn_config, is_vip FROM users WHERE sub_id = ?",
                (sub_id,)
            ).fetchone()
            if u_row:
                user_id = u_row["user_id"]
                client_uuid = u_row["client_uuid"]
                exp_str = u_row["vip_until"] or ""
                cfg_saved = u_row["vpn_config"] or ""
                is_active = u_row["is_vip"]
            else:
                return 404, {"Content-Type": "text/plain; charset=utf-8"}, b"Subscription not found"

        # Список неактивных серверов для моментального исключения из выдачи
        inactive_srvs = []
        try:
            cur.execute("SELECT name, host FROM servers WHERE is_active = 0")
            inactive_srvs = cur.fetchall()
        except Exception:
            pass

    # Проверка активности подписки
    if not is_active:
        msg = "⚠️ Ваша подписка истекла или отключена. Для продления перейдите в бота."
        b64_empty = base64.b64encode(msg.encode("utf-8"))
        return 200, {
            "Content-Type": "text/plain; charset=utf-8",
            "Subscription-Userinfo": f"upload=0; download={traffic_bytes}; total={int(lim_gb*1073741824)}; expire=0"
        }, b64_empty

    # 2. Проверка лимита устройств по HWID (если передан x-hwid)
    req_hwid = headers.get("x-hwid") or headers.get("X-HWID") or ""
    if req_hwid and k_id:
        try:
            with get_db_conn(p) as conn:
                max_dev = 2 if ("2устро" in grp_name.lower() or "2 устр" in grp_name.lower()) else 1
                known = [r[0] for r in conn.execute("SELECT hwid FROM key_devices WHERE key_id = ?", (k_id,)).fetchall()]
                if req_hwid not in known:
                    if len(known) >= max_dev:
                        warn_node = "vless://00000000-0000-0000-0000-000000000000@127.0.0.1:443?encryption=none&type=tcp#%E2%9B%94%20%D0%9B%D0%B8%D0%BC%D0%B8%D1%82%20%D1%83%D1%81%D1%82%D1%80%D0%BE%D0%B9%D1%81%D1%82%D0%B2%20%D0%B8%D1%81%D1%87%D0%B5%D1%80%D0%BF%D0%B0%D0%BD!%20%D0%A1%D0%B1%D1%80%D0%BE%D1%81%D1%8C%D1%82%D0%B5%20%D0%B2%20%D0%B1%D0%BE%D1%82%D0%B5%20(20%E2%82%BD)"
                        return 200, {"Content-Type": "text/plain; charset=utf-8"}, base64.b64encode(warn_node.encode("utf-8"))
                    else:
                        conn.execute(
                            "INSERT OR IGNORE INTO key_devices (key_id, sub_token, hwid, last_ip) VALUES (?, ?, ?, ?)",
                            (k_id, sub_id, req_hwid, client_ip)
                        )
                        conn.commit()
        except Exception:
            pass

    # 3. Формирование актуальных конфигов
    # Фильтрация битых или неполных Reality ссылок и моментальное исключение отключенных серверов (is_active = 0)
    cleaned_lines = []
    for line in cfg_saved.splitlines():
        l_str = line.strip()
        if not l_str:
            continue
        if "security=reality" in l_str and ("pbk=&" in l_str or l_str.endswith("pbk=")):
            logger.warning(f"Пропуск повреждённой Reality ссылки без pbk: {l_str[:50]}...")
            continue

        # Моментальное исключение конфигураций серверов с is_active = 0
        srv_disabled = False
        for s_row in (inactive_srvs or []):
            try:
                s_name = s_row[0]
                s_host = s_row[1]
            except Exception:
                s_name = str(s_row)
                s_host = ""
            clean_host = (s_host or "").split(":")[0].strip()
            if clean_host and clean_host in l_str:
                srv_disabled = True
                break
            if s_name and f"#{s_name}" in l_str:
                srv_disabled = True
                break
        if srv_disabled:
            continue

        cleaned_lines.append(l_str)

    final_configs = "\n".join(cleaned_lines).strip()
    if not final_configs:
        final_configs = f"vless://{client_uuid}@ger.svoii-vpn.online:443?security=tls#{loc_name}"

    # 4. Расчет метаданных для заголовков и веб-дашборда
    expire_ts = 0
    days_left_str = "∞"
    status_str = "Активен"
    expire_fmt_str = "Бессрочно"
    if exp_str and exp_str != "Без срока":
        try:
            exp_clean = exp_str.split(".")[0]
            dt_fmt = "%Y-%m-%d %H:%M:%S" if " " in exp_clean else "%Y-%m-%d"
            exp_dt = datetime.strptime(exp_clean, dt_fmt)
            expire_ts = int(exp_dt.timestamp())
            expire_fmt_str = exp_dt.strftime("%d.%m.%Y %H:%M")
            delta = exp_dt - datetime.now()
            if delta.total_seconds() > 0:
                if delta.days > 0:
                    days_left_str = f"{delta.days} дн."
                else:
                    days_left_str = f"{max(1, int(delta.total_seconds() // 3600))} ч."
            else:
                days_left_str = "Истёк"
                status_str = "Истёк"
        except Exception:
            pass

    limit_bytes = int(lim_gb * 1073741824) if lim_gb > 0 else 0
    total_gb_str = f"{traffic_bytes / 1073741824:.2f} GB"
    limit_gb_str = f"{lim_gb:.0f} GB" if lim_gb > 0 else "Неограниченно"

    resp_headers = {
        "Subscription-Userinfo": f"upload=0; download={traffic_bytes}; total={limit_bytes}; expire={expire_ts}",
        "Profile-Title": f"base64:{base64.b64encode('SVOII VPN'.encode('utf-8')).decode('utf-8')}",
        "Profile-Update-Interval": "12",
        "Profile-Update-Interval": "12"
    }

    # 5. Определение клиента (браузер vs VPN-клиент)
    acc_hdr = (headers.get("Accept") or headers.get("accept") or "").lower()
    ua_hdr = (headers.get("User-Agent") or headers.get("user-agent") or "").lower()
    is_vpn_client = any(app in ua_hdr for app in [
        "v2ray", "happ", "streisand", "hiddify", "shadowrocket",
        "sing-box", "clash", "nekobox", "karing", "flclash", "curl", "wget"
    ])
    is_browser = ("text/html" in acc_hdr or "mozilla" in ua_hdr) and not is_vpn_client

    # Если открыто в браузере и не запрошен сырой формат (raw=1 или format=raw)
    if is_browser and "raw=1" not in query and "format=raw" not in query:
        try:
            import sub_dashboard
            sub_url = get_clean_sub_url(sub_id, p)
            u_info = {
                "name": f"Клиент #{k_id or user_id}",
                "up_gb": "0.00 GB",
                "down_gb": total_gb_str,
                "total_gb": total_gb_str,
                "limit_gb": limit_gb_str,
                "days_left": days_left_str,
                "expire_str": expire_fmt_str,
                "status": status_str,
                "last_online": "Онлайн"
            }
            html_content = sub_dashboard.build_sub_dashboard_html(sub_url, sub_id, u_info, final_configs)
            resp_headers["Content-Type"] = "text/html; charset=utf-8"
            return 200, resp_headers, html_content.encode("utf-8")
        except Exception as e:
            logger.error(f"Dashboard generation fallback error: {e}")

    # Для VPN-клиентов отдаём Base64
    b64_output = base64.b64encode(final_configs.encode("utf-8"))
    resp_headers["Content-Type"] = "text/plain; charset=utf-8"
    return 200, resp_headers, b64_output


class SubServerHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        try:
            parts = [p for p in self.path.split("?")[0].split("/") if p]
            if not parts:
                self.send_error(404, "Not Found")
                return

            sub_id = parts[-1]
            query = self.path.split("?")[1] if "?" in self.path else ""
            status, headers, body = build_subscription_payload(
                sub_id,
                headers=dict(self.headers),
                query=query,
                client_ip=self.client_address[0]
            )

            self.send_response(status)
            for hk, hv in headers.items():
                self.send_header(hk, hv)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            logger.error(f"SubServerHandler error: {e}")
            try:
                self.send_error(500, "Internal Server Error")
            except Exception:
                pass


def start_sub_server(port: int = 2097, db_path: str = DB_PATH):
    global DB_PATH
    DB_PATH = db_path
    try:
        from migrate_db import run_migrations
        run_migrations(db_path)
    except Exception as e:
        logger.warning(f"sub_server migration notice: {e}")
    auto_heal_sub_database(db_path)

    def run():
        try:
            srv = HTTPServer(("0.0.0.0", port), SubServerHandler)
            srv.serve_forever()
        except Exception as e:
            logger.error(f"Ошибка старта sub_server: {e}")

    t = threading.Thread(target=run, daemon=True, name="SubServerThread")
    t.start()
    return t

format_sub_url = get_clean_sub_url
