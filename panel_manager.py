import os
import json
import sqlite3
import logging
import asyncio
import urllib.parse
from datetime import datetime
import httpx

from wpp_client import WPPClient

logger = logging.getLogger("Bot_VIP.PanelManager")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("DB_PATH", os.path.join(BASE_DIR, "bot_data.db"))
_sync_lock = asyncio.Lock()

def get_db_conn(db_path: str = None):
    p = db_path or DB_PATH
    conn = sqlite3.connect(p, timeout=30.0)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.row_factory = sqlite3.Row
    return conn


def parse_stream_settings(raw_stream) -> dict:
    if isinstance(raw_stream, dict):
        return raw_stream
    if isinstance(raw_stream, str) and raw_stream.strip():
        try:
            parsed = json.loads(raw_stream)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}

def generate_3xui_link(inb: dict, client_uuid: str, host: str, country_flag: str = "🇵🇱", default_flow: str = "xtls-rprx-vision") -> str:
    """
    Генерация правильной ссылки подключения (VLESS, VMess, Trojan, Shadowsocks)
    с полным извлечением параметров Reality pbk, sni, sid, spx и независимым разбором транспорта и безопасности.
    """
    # 1. Проверка активности инбаунда
    if inb.get("enable") is False:
        return ""
    
    remark = str(inb.get("remark") or "").strip()
    if remark.startswith("--!"):
        return ""  # Служебный/игнорируемый инбаунд

    proto = str(inb.get("protocol") or "vless").lower().strip()
    port = inb.get("port")
    if not port:
        return ""

    if country_flag and not remark.startswith(country_flag):
        remark = f"{country_flag} {remark}".strip()

    stream = parse_stream_settings(inb.get("streamSettings"))
    net = str(stream.get("network") or "tcp").lower().strip()
    sec = str(stream.get("security") or "none").lower().strip()

    params = [f"type={net}"]

    # 2. Обработка транспорта (независимо от шифрования)
    if net == "ws":
        ws_sett = stream.get("wsSettings") or {}
        path = ws_sett.get("path", "/")
        headers = ws_sett.get("headers") or {}
        ws_host = headers.get("Host") or host
        params.append(f"path={urllib.parse.quote(path, safe='/')}")
        params.append(f"host={ws_host}")
    elif net == "grpc":
        grpc_sett = stream.get("grpcSettings") or {}
        service_name = grpc_sett.get("serviceName", "")
        if service_name:
            params.append(f"serviceName={urllib.parse.quote(service_name)}")
    elif net == "tcp":
        tcp_sett = stream.get("tcpSettings") or {}
        header_type = (tcp_sett.get("header") or {}).get("type", "none")
        if header_type != "none":
            params.append(f"headerType={header_type}")
    elif net in ("http", "h2", "xhttp"):
        http_sett = stream.get("httpSettings") or stream.get("xhttpSettings") or {}
        path = http_sett.get("path", "/")
        params.append(f"path={urllib.parse.quote(path, safe='/')}")

    # 3. Обработка безопасности (Reality / TLS / None)
    if sec != "none":
        params.append(f"security={sec}")

    if sec == "reality":
        real_sett = stream.get("realitySettings") or {}
        nested_sett = real_sett.get("settings") or {}
        
        # Точное извлечение Public Key: проверяем settings.publicKey, затем верхний уровень
        pbk = nested_sett.get("publicKey") or real_sett.get("publicKey") or ""
        fp = nested_sett.get("fingerprint") or real_sett.get("fingerprint") or "chrome"
        spx = nested_sett.get("spiderX") or real_sett.get("spiderX") or "/"
        
        # SNI из serverNames
        server_names = real_sett.get("serverNames") or nested_sett.get("serverNames") or []
        sni = server_names[0] if server_names else host
        
        # ShortIds
        short_ids = real_sett.get("shortIds") or []
        sid = short_ids[0] if short_ids else ""

        if pbk:
            params.append(f"pbk={pbk}")
        else:
            logger.warning(f"Reality inbound port={port} has empty publicKey!")
            
        params.append(f"fp={fp}")
        params.append(f"sni={sni}")
        if sid:
            params.append(f"sid={sid}")
        if spx:
            params.append(f"spx={urllib.parse.quote(spx, safe='/')}")

        # Для VLESS Reality добавляем Flow (xtls-rprx-vision) если сеть TCP
        if proto == "vless" and net == "tcp":
            params.append(f"flow={default_flow}")

    elif sec == "tls":
        tls_sett = stream.get("tlsSettings") or {}
        sni = tls_sett.get("serverName") or host
        params.append(f"sni={sni}")
        alpn = tls_sett.get("alpn")
        if alpn:
            alpn_str = ",".join(alpn) if isinstance(alpn, list) else str(alpn)
            params.append(f"alpn={urllib.parse.quote(alpn_str)}")
        if proto == "vless" and net == "tcp":
            params.append(f"flow={default_flow}")

    query_str = "&".join(params)
    return f"{proto}://{client_uuid}@{host}:{port}?{query_str}#{urllib.parse.quote(remark)}"


class XUIAdapter:
    """Адаптер для работы с 3X-UI через REST API (Cookie + CSRF / Bearer)"""
    def __init__(self, base_url: str, username: str, password: str, timeout: float = 8.0):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.cookies = {}
        self.csrf_token = ""
        self._auth_lock = asyncio.Lock()

    async def _login(self, client: httpx.AsyncClient) -> bool:
        async with self._auth_lock:
            try:
                csrf_res = await client.get(f"{self.base_url}/csrf-token")
                token = csrf_res.json().get("obj", "") if csrf_res.status_code == 200 else ""
                self.csrf_token = token

                _origin = self.base_url.split('/oubex')[0] if '/oubex' in self.base_url else self.base_url
                headers = {"Referer": f"{self.base_url}/", "Origin": _origin}
                if token:
                    headers["X-CSRF-Token"] = token

                login_resp = await client.post(
                    f"{self.base_url}/login",
                    data={"username": self.username, "password": self.password},
                    headers=headers
                )
                if login_resp.status_code == 200 and login_resp.json().get("success", True):
                    self.cookies = dict(login_resp.cookies)
                    return True
                logger.error(f"3X-UI login failed for {self.base_url}: HTTP {login_resp.status_code}")
                return False
            except Exception as e:
                logger.error(f"3X-UI connection error during login {self.base_url}: {e}")
                return False

    async def get_inbounds(self) -> list:
        try:
            async with httpx.AsyncClient(verify=False, timeout=self.timeout, cookies=self.cookies) as client:
                headers = {"X-CSRF-Token": self.csrf_token} if self.csrf_token else {}
                resp = await client.get(f"{self.base_url}/panel/api/inbounds/list", headers=headers)
                if resp.status_code == 401 or not resp.text.strip():
                    if await self._login(client):
                        headers["X-CSRF-Token"] = self.csrf_token
                        resp = await client.get(f"{self.base_url}/panel/api/inbounds/list", headers=headers)
                
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("obj", []) or []
                return []
        except Exception as e:
            logger.warning(f"Error fetching inbounds from {self.base_url}: {e}")
            return []

    async def add_or_update_client(self, client_uuid: str, email: str, sub_id: str, expiry_ms: int = 0, limit_ip: int = 1, total_gb: int = 0) -> tuple[bool, str]:
        try:
            async with httpx.AsyncClient(verify=False, timeout=self.timeout, cookies=self.cookies) as client:
                headers = {"X-CSRF-Token": self.csrf_token, "Content-Type": "application/json"}
                inbounds = await self.get_inbounds()
                if not inbounds:
                    return False, "Инбаунды не найдены"

                target_ids = [inb["id"] for inb in inbounds if inb.get("enable") is not False]
                payload = {
                    "inboundIds": target_ids,
                    "client": {
                        "id": client_uuid,
                        "inboundIds": target_ids,
                        "email": email,
                        "enable": True,
                        "subId": str(sub_id),
                        "limitIp": limit_ip,
                        "totalGB": total_gb,
                        "expiryTime": expiry_ms,
                        "flow": "xtls-rprx-vision"
                    }
                }
                # Пробуем через /panel/api/clients/add
                resp = await client.post(f"{self.base_url}/panel/api/clients/add", json=payload, headers=headers)
                if resp.status_code in (200, 201):
                    return True, "Клиент добавлен"
                
                # Фоллбэк на /panel/api/inbounds/addClient для каждого инбаунда
                added_any = False
                for inb in inbounds:
                    inb_id = inb["id"]
                    proto = inb.get("protocol", "").lower()
                    client_entry = {
                        "id": client_uuid,
                        "email": email,
                        "subId": str(sub_id),
                        "limitIp": limit_ip,
                        "totalGB": total_gb,
                        "expiryTime": expiry_ms,
                        "enable": True
                    }
                    if proto == "vless":
                        client_entry["flow"] = "xtls-rprx-vision"
                    
                    single_payload = {
                        "id": inb_id,
                        "settings": json.dumps({"clients": [client_entry]})
                    }
                    s_resp = await client.post(f"{self.base_url}/panel/api/inbounds/addClient", json=single_payload, headers=headers)
                    if s_resp.status_code == 200 and s_resp.json().get("success"):
                        added_any = True
                
                return added_any, "Клиент добавлен в инбаунды" if added_any else f"HTTP {resp.status_code}"
        except Exception as e:
            return False, str(e)

    async def get_client_traffic(self, email: str) -> dict:
        """Сбор трафика пользователя по email из 3X-UI"""
        try:
            encoded = urllib.parse.quote(email, safe="")
            async with httpx.AsyncClient(verify=False, timeout=self.timeout, cookies=self.cookies) as client:
                headers = {"X-CSRF-Token": self.csrf_token} if self.csrf_token else {}
                resp = await client.get(f"{self.base_url}/panel/api/clients/traffic/{encoded}", headers=headers)
                if resp.status_code == 401:
                    if await self._login(client):
                        headers["X-CSRF-Token"] = self.csrf_token
                        resp = await client.get(f"{self.base_url}/panel/api/clients/traffic/{encoded}", headers=headers)
                if resp.status_code == 200:
                    data = resp.json().get("obj") or {}
                    up = int(data.get("up", 0))
                    down = int(data.get("down", 0))
                    return {"up": up, "down": down, "total": up + down}
        except Exception as e:
            logger.debug(f"3X-UI traffic check error for {email}: {e}")
        return {"up": 0, "down": 0, "total": 0}

    async def set_client_enabled(self, email: str, enable: bool) -> bool:
        """Включение/отключение клиента в 3X-UI"""
        try:
            encoded = urllib.parse.quote(email, safe="")
            endpoint = "attach" if enable else "detach"
            async with httpx.AsyncClient(verify=False, timeout=self.timeout, cookies=self.cookies) as client:
                headers = {"X-CSRF-Token": self.csrf_token} if self.csrf_token else {}
                resp = await client.post(f"{self.base_url}/panel/api/clients/{encoded}/{endpoint}", headers=headers)
                return resp.status_code == 200
        except Exception:
            return False

    async def delete_client(self, email: str) -> bool:
        try:
            encoded = urllib.parse.quote(email, safe="")
            async with httpx.AsyncClient(verify=False, timeout=self.timeout, cookies=self.cookies) as client:
                headers = {"X-CSRF-Token": self.csrf_token} if self.csrf_token else {}
                resp = await client.post(f"{self.base_url}/panel/api/clients/delete/{encoded}", headers=headers)
                return resp.status_code == 200
        except Exception:
            return False


class ProvisionResult:
    """Результат провижининга с поддержкой итерирования как списка ссылок и доступа к профилям нод"""
    def __init__(self, links: list[str] = None, node_entries: list[dict] = None):
        self.links = links or []
        self.node_entries = node_entries or []

    def __iter__(self):
        return iter(self.links)

    def __getitem__(self, item):
        return self.links[item]

    def __len__(self):
        return len(self.links)

    def __contains__(self, item):
        return item in self.links

    def get(self, key, default=None):
        if key == "links":
            return self.links
        if key == "node_entries":
            return self.node_entries
        return default

    def __repr__(self):
        return f"<ProvisionResult links={len(self.links)} nodes={len(self.node_entries)}>"


class MultiPanelManager:
    """Единый оркестратор взаимодействия с нодами 3X-UI и WPP"""

    @staticmethod
    def normalize_server_dict(server_row) -> dict:
        if isinstance(server_row, dict):
            return dict(server_row)
        d = {}
        if hasattr(server_row, "keys"):
            d = dict(server_row)
        elif isinstance(server_row, (list, tuple)):
            cols = [
                "id", "name", "host", "panel_type", "panel_url", "panel_user", "panel_pass",
                "inbound_id", "sub_url_prefix", "proxy_template", "is_active", "created_at", "group_name"
            ]
            for idx, col in enumerate(cols):
                if idx < len(server_row):
                    d[col] = server_row[idx]

        srv_id = d.get("id")
        if srv_id:
            try:
                import sqlite3, os
                db_path = os.getenv("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_data.db"))
                if os.path.exists(db_path):
                    with sqlite3.connect(db_path, timeout=5.0) as conn:
                        conn.row_factory = sqlite3.Row
                        row = conn.execute("SELECT * FROM servers WHERE id = ?", (srv_id,)).fetchone()
                        if row:
                            for k in row.keys():
                                if k not in d or d[k] is None or d[k] == "":
                                    d[k] = row[k]
            except Exception:
                pass
        return d

    @classmethod
    async def provision_on_server(cls, server: dict, user_id: int, key_id: int, client_uuid: str, sub_id: str, expiry_ms: int = 0) -> ProvisionResult:
        """
        Универсальное добавление клиента на ноду (3X-UI или WPP).
        Возвращает ProvisionResult со списком ссылок и метаданными профилей для БД.
        """
        srv = cls.normalize_server_dict(server)
        srv_id = srv.get("id", 0)
        p_type = str(srv.get("panel_type") or "3xui").lower()
        panel_url = srv.get("panel_url", "").strip()
        server_host = srv.get("host", "").split(":")[0] or "127.0.0.1"
        country_flag = srv.get("name", "").split(" ")[0] if srv.get("name") else "🌐"
        if len(country_flag) > 4:
            country_flag = "🌐"

        generated = []
        node_entries = []
        email = f"user_{user_id}_k{key_id}"

        # 1. 3X-UI Нода
        if p_type in ("3xui", "3x-ui", "both") and panel_url:
            try:
                adapter = XUIAdapter(panel_url, srv.get("panel_user", ""), srv.get("panel_pass", ""))
                ok, msg = await adapter.add_or_update_client(client_uuid, email, sub_id, expiry_ms=expiry_ms)
                inbounds = await adapter.get_inbounds()
                for inb in inbounds:
                    link = generate_3xui_link(inb, client_uuid, server_host, country_flag)
                    if link:
                        generated.append(link)
                node_entries.append({
                    "server_id": srv_id,
                    "panel_type": "3xui",
                    "remote_id": client_uuid,
                    "remote_email": email
                })
            except Exception as e:
                logger.warning(f"Failed provisioning on 3X-UI node {srv.get('name')}: {e}")

        # 2. WPP Нода
        if p_type in ("wpp", "web_panel_proxy", "both"):
            try:
                # При panel_type='both' — проверяем наличие wpp_token перед провижинингом.
                # Если wpp_token пуст — пропускаем WPP без ошибки 401.
                if p_type == "both":
                    _wpp_token = (srv.get("wpp_token") or "").strip()
                    if not _wpp_token:
                        logger.warning(
                            f"WPP provisioning skipped for server '{srv.get('name')}' (panel_type='both'): "
                            f"wpp_token is empty. Set wpp_token in servers table to enable WPP."
                        )
                    else:
                        wpp = WPPClient(server=srv)
                        res = await wpp.create_client_bundle_async(user_id, key_id=key_id, protocols=["vless"])
                        if res.get("configs"):
                            generated.extend(res["configs"])
                        
                        profiles = res.get("profiles", [])
                        if profiles:
                            for prof in profiles:
                                node_entries.append({
                                    "server_id": srv_id,
                                    "panel_type": "wpp",
                                    "remote_id": str(prof.get("id") or f"{user_id}_{key_id}"),
                                    "remote_email": email
                                })
                        else:
                            node_entries.append({
                                "server_id": srv_id,
                                "panel_type": "wpp",
                                "remote_id": f"{user_id}_{key_id}",
                                "remote_email": email
                            })
                else:
                    wpp = WPPClient(server=srv)
                    res = await wpp.create_client_bundle_async(user_id, key_id=key_id, protocols=["vless"])
                    if res.get("configs"):
                        generated.extend(res["configs"])
                    
                    profiles = res.get("profiles", [])
                    if profiles:
                        for prof in profiles:
                            node_entries.append({
                                "server_id": srv_id,
                                "panel_type": "wpp",
                                "remote_id": str(prof.get("id") or f"{user_id}_{key_id}"),
                                "remote_email": email
                            })
                    else:
                        node_entries.append({
                            "server_id": srv_id,
                            "panel_type": "wpp",
                            "remote_id": f"{user_id}_{key_id}",
                            "remote_email": email
                        })
            except Exception as e:
                logger.warning(f"Failed provisioning on WPP node {srv.get('name')}: {e}")

        return ProvisionResult(generated, node_entries)

    @classmethod
    async def fetch_traffic_from_server(cls, server: dict, user_id: int, key_id: int, remote_email: str = None, remote_id: str = None) -> dict:
        """Сбор трафика с ноды с отказоустойчивостью"""
        srv = cls.normalize_server_dict(server)
        p_type = str(srv.get("panel_type") or "3xui").lower()
        panel_url = srv.get("panel_url", "")
        email = remote_email or f"user_{user_id}_k{key_id}"
        total_up = 0
        total_down = 0

        # Опрос 3X-UI
        if p_type in ("3xui", "3x-ui", "both") and panel_url:
            try:
                adapter = XUIAdapter(panel_url, srv.get("panel_user", ""), srv.get("panel_pass", ""))
                res = await adapter.get_client_traffic(email)
                total_up += int(res.get("up", 0))
                total_down += int(res.get("down", 0))
            except Exception as e:
                logger.debug(f"3X-UI traffic poll error on {srv.get('name')}: {e}")

        # Опрос WPP
        if p_type in ("wpp", "web_panel_proxy", "both"):
            try:
                wpp = WPPClient(server=srv)
                res = await wpp.get_profile_traffic_async(remote_id or f"{user_id}_{key_id}")
                total_up += int(res.get("up", 0))
                total_down += int(res.get("down", 0))
            except Exception as e:
                logger.debug(f"WPP traffic poll error on {srv.get('name')}: {e}")

        return {"up": total_up, "down": total_down, "total": total_up + total_down}

    @classmethod
    async def deprovision_on_server(cls, server: dict, user_id: int, key_id: int, client_uuid: str = "", remote_email: str = None, remote_id: str = None) -> bool:
        """Полное удаление старого клиента с ноды (3X-UI и WPP) при ротации UUID / перевыпуске ссылки"""
        srv = cls.normalize_server_dict(server)
        p_type = str(srv.get("panel_type") or "3xui").lower()
        panel_url = srv.get("panel_url", "")
        email = remote_email or f"user_{user_id}_k{key_id}"

        # Удаление из 3X-UI
        if p_type in ("3xui", "3x-ui", "both") and panel_url:
            try:
                adapter = XUIAdapter(panel_url, srv.get("panel_user", ""), srv.get("panel_pass", ""))
                await adapter.delete_client(email)
            except Exception as e:
                logger.warning(f"Error deprovisioning client on 3X-UI ({srv.get('name')}): {e}")

        # Удаление из WPP
        if p_type in ("wpp", "web_panel_proxy", "both"):
            try:
                wpp = WPPClient(server=srv)
                await wpp.delete_profile_async(remote_id or f"{user_id}_{key_id}")
            except Exception as e:
                logger.warning(f"Error deprovisioning client on WPP ({srv.get('name')}): {e}")

        return True

    @classmethod
    async def deactivate_on_server(cls, server: dict, user_id: int, key_id: int, remote_email: str = None, remote_id: str = None) -> bool:
        """Отключение клиента на ноде при окончании подписки или исчерпании лимита"""
        srv = cls.normalize_server_dict(server)
        p_type = str(srv.get("panel_type") or "3xui").lower()
        panel_url = srv.get("panel_url", "")
        email = remote_email or f"user_{user_id}_k{key_id}"

        if p_type in ("3xui", "3x-ui", "both") and panel_url:
            try:
                adapter = XUIAdapter(panel_url, srv.get("panel_user", ""), srv.get("panel_pass", ""))
                await adapter.set_client_enabled(email, enable=False)
            except Exception as e:
                logger.warning(f"Error deactivating client on 3X-UI ({srv.get('name')}): {e}")

        if p_type in ("wpp", "web_panel_proxy", "both"):
            try:
                wpp = WPPClient(server=srv)
                await wpp.delete_profile_async(remote_id or f"{user_id}_{key_id}")
            except Exception as e:
                logger.warning(f"Error deactivating client on WPP ({srv.get('name')}): {e}")

        return True


def get_servers_for_group(group_name: str = "", db_path: str = None) -> list[dict]:
    """
    Возвращает список активных серверов, соответствующих группе тарифа.
    Строгая изоляция: клиенты группы 'Стандарт' не получают ноды из 'VIP'/'Proxy'.
    """
    p = db_path or DB_PATH
    if not os.path.exists(p):
        return []
    with get_db_conn(p) as conn:
        all_active_rows = conn.execute("SELECT * FROM servers WHERE is_active = 1").fetchall()
        all_active = [dict(r) for r in all_active_rows]
        if not all_active:
            return []
        grp = (group_name or "").strip()
        if not grp:
            return all_active

        matched = []
        for s in all_active:
            s_grp = (s.get("group_name") or "").strip()
            groups = [g.strip() for g in s_grp.split(",") if g.strip()]
            if grp in groups:
                matched.append(s)
        if matched:
            return matched

        # Фоллбэк только на неназначенные/общедоступные серверы
        unassigned = [
            s for s in all_active
            if not s.get("group_name") or s.get("group_name").strip() in ("", "Все", "Общие")
        ]
        if unassigned:
            return unassigned

        return []


async def sync_active_keys_with_servers(db_path: str = None) -> dict:
    """
    Синхронизирует активные ключи со всеми активными серверами:
    - Добавляет ключи на новые серверы по матрице тарифа
    - Проверяет инбаунды и пересобирает ссылки Reality/TLS
    - Деактивирует привязки на отключённых/удалённых серверах
    - Обновляет config_text в user_keys при изменении ссылок
    Защищена глобальным asyncio.Lock от наложения двух циклов.
    """
    p = db_path or DB_PATH
    if _sync_lock.locked():
        logger.warning("[sync_active_keys] Предыдущая синхронизация ещё выполняется, пропускаем цикл.")
        return {"skipped": True, "reason": "locked"}

    async with _sync_lock:
        stats = {"processed": 0, "provisioned": 0, "updated": 0, "deactivated": 0, "errors": 0}
        if not os.path.exists(p):
            return stats

        with get_db_conn(p) as conn:
            active_servers_rows = conn.execute("SELECT * FROM servers WHERE is_active = 1").fetchall()
            active_servers = [dict(r) for r in active_servers_rows]
            active_srv_ids = {s["id"] for s in active_servers}

            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            keys_rows = conn.execute(
                "SELECT * FROM user_keys WHERE is_active = 1 AND (expires_at = 'Без срока' OR expires_at > ?)",
                (now_str,)
            ).fetchall()
            keys = [dict(r) for r in keys_rows]

        logger.info(f"[sync_active_keys] Старт синхронизации: {len(keys)} активных ключей, {len(active_servers)} серверов")

        for key in keys:
            k_id = key["id"]
            user_id = key["user_id"]
            client_uuid = key["client_uuid"]
            sub_id = key["sub_id"]
            grp = key.get("group_name") or ""
            exp_str = key.get("expires_at") or ""
            old_config = key.get("config_text") or ""

            expiry_ms = 0
            if exp_str and exp_str != "Без срока":
                try:
                    exp_clean = exp_str.split(".")[0]
                    dt_fmt = "%Y-%m-%d %H:%M:%S" if " " in exp_clean else "%Y-%m-%d"
                    exp_dt = datetime.strptime(exp_clean, dt_fmt)
                    expiry_ms = int(exp_dt.timestamp() * 1000)
                except Exception:
                    pass

            target_servers = get_servers_for_group(grp, db_path=p)
            target_srv_ids = {s["id"] for s in target_servers}

            with get_db_conn(p) as conn:
                existing_nodes = conn.execute(
                    "SELECT * FROM user_key_nodes WHERE key_id = ?", (k_id,)
                ).fetchall()
                existing_map = {r["server_id"]: dict(r) for r in existing_nodes}

            key_links = []

            for srv in target_servers:
                sid = srv["id"]
                node_rec = existing_map.get(sid)

                try:
                    res = await MultiPanelManager.provision_on_server(
                        srv, user_id, k_id, client_uuid, sub_id, expiry_ms
                    )
                    if res.links:
                        key_links.extend(res.links)

                    with get_db_conn(p) as conn:
                        for ne in res.node_entries:
                            p_type = ne.get("panel_type", "3xui")
                            r_id = ne.get("remote_id", "")
                            r_email = ne.get("remote_email", f"user_{user_id}_k{k_id}")
                            cur = conn.execute(
                                "SELECT id FROM user_key_nodes WHERE key_id = ? AND server_id = ? AND panel_type = ?",
                                (k_id, sid, p_type)
                            )
                            exist_row = cur.fetchone()
                            if exist_row:
                                conn.execute(
                                    "UPDATE user_key_nodes SET remote_id = ?, remote_email = ?, is_active = 1, updated_at = ? WHERE id = ?",
                                    (r_id, r_email, now_str, exist_row["id"])
                                )
                            else:
                                conn.execute(
                                    "INSERT INTO user_key_nodes (key_id, server_id, panel_type, remote_id, remote_email, is_active, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?)",
                                    (k_id, sid, p_type, r_id, r_email, now_str)
                                )
                        conn.commit()

                    if not node_rec:
                        stats["provisioned"] += 1
                    else:
                        stats["updated"] += 1

                except Exception as e:
                    logger.warning(f"[sync_active_keys] Ошибка ноды #{sid} для ключа #{k_id}: {e}")
                    stats["errors"] += 1

                await asyncio.sleep(0.2)

            with get_db_conn(p) as conn:
                for sid, n_info in existing_map.items():
                    if sid not in target_srv_ids or sid not in active_srv_ids:
                        if n_info.get("is_active", 1):
                            conn.execute(
                                "UPDATE user_key_nodes SET is_active = 0, updated_at = ? WHERE key_id = ? AND server_id = ?",
                                (now_str, k_id, sid)
                            )
                            conn.commit()
                            stats["deactivated"] += 1

            if key_links:
                new_combined = "\n".join(key_links).strip()
                if new_combined and new_combined != old_config.strip():
                    with get_db_conn(p) as conn:
                        conn.execute("UPDATE user_keys SET config_text = ? WHERE id = ?", (new_combined, k_id))
                        conn.commit()

            stats["processed"] += 1

        logger.info(f"[sync_active_keys] Синхронизация завершена: {stats}")
        return stats

