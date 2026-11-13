import os
import json
import logging
import asyncio
import httpx

logger = logging.getLogger("Bot_VIP.WPPClient")

DEFAULT_WPP_URL = "http://127.0.0.1:8090"
DEFAULT_WPP_KEY = "Lw8RaZZKE095ccmS07xSfGshnGiS3U3JiRnkqpOP5b0"

def resolve_wpp_credentials(server: dict = None, base_url: str = None, api_key: str = None) -> tuple[str, str]:
    """
    Разрешает корректный URL и API-токен для ноды WPP (Web-Panel-Proxy):
    1. Явно переданные параметры base_url / api_key
    2. Из объекта сервера (wpp_url / wpp_token, либо panel_pass если panel_type == 'wpp')
    3. Из переменных окружения (.env: WPP_URL, WPP_API_KEY, WPP_TOKEN)
    4. Из таблицы settings БД (wpp_url, wpp_api_key)
    5. Fallback на значения по умолчанию
    """
    url = (base_url or "").strip()
    token = (api_key or "").strip()

    if isinstance(server, dict):
        if not url:
            url = (server.get("wpp_url") or "").strip()
        if not token:
            token = (server.get("wpp_token") or "").strip()

        p_type = str(server.get("panel_type") or "").lower()
        # ВАЖНО: при panel_type='both' используем только wpp_url/wpp_token.
        # panel_pass содержит пароль 3X-UI и НЕ должен использоваться как WPP токен.
        # При panel_type='wpp' (одиночная WPP нода) — разрешаем фоллбэк на panel_pass.
        if p_type in ("wpp", "web_panel_proxy"):
            if not url:
                url = (server.get("panel_url") or "").strip()
            if not token:
                token = (server.get("panel_pass") or "").strip()
        # Для panel_type='both': НЕ читаем panel_pass/panel_url как WPP credentials.
        # Если wpp_token пуст — логируем WARNING, токен останется пустым и клиент
        # вернёт ошибку 401, которую вызывающий код должен обработать через guard ниже.

    if not url:
        url = os.getenv("WPP_URL", "").strip()
    if not token:
        token = os.getenv("WPP_API_KEY", "").strip() or os.getenv("WPP_TOKEN", "").strip()

    if not url or not token:
        try:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            db_path = os.getenv("DB_PATH", os.path.join(base_dir, "bot_data.db"))
            if os.path.exists(db_path):
                import sqlite3
                with sqlite3.connect(db_path, timeout=5.0) as conn:
                    if not url:
                        r = conn.execute("SELECT value FROM settings WHERE key='wpp_url'").fetchone()
                        if r and r[0]:
                            url = r[0].strip()
                    if not token:
                        r = conn.execute("SELECT value FROM settings WHERE key='wpp_api_key'").fetchone()
                        if r and r[0]:
                            token = r[0].strip()
        except Exception:
            pass

    final_url = url or DEFAULT_WPP_URL
    final_token = token or DEFAULT_WPP_KEY
    return final_url.rstrip("/"), final_token


class WPPClient:
    def __init__(self, base_url: str = None, api_key: str = None, timeout: float = 8.0, server: dict = None):
        res_url, res_token = resolve_wpp_credentials(server=server, base_url=base_url, api_key=api_key)
        self.base_url = res_url.rstrip("/")

        # Корректная обработка Bearer префикса
        raw_key = res_token.strip()
        if raw_key.lower().startswith("bearer "):
            bearer_val = raw_key
            token_val = raw_key[7:].strip()
        else:
            bearer_val = f"Bearer {raw_key}" if raw_key else ""
            token_val = raw_key

        self.api_key = token_val
        self.timeout = timeout
        self.headers = {
            "Authorization": bearer_val,
            "X-API-Key": token_val,
            "Content-Type": "application/json"
        }

    def _mask_token(self) -> str:
        if not self.api_key:
            return "<empty>"
        if len(self.api_key) <= 8:
            return "***"
        return f"{self.api_key[:4]}...{self.api_key[-4:]}"

    async def _request_async(self, endpoint: str, method: str = "GET", payload: dict = None) -> dict:
        url = f"{self.base_url}/wpp-api/v1{endpoint}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout, verify=False) as client:
                if method.upper() == "POST":
                    resp = await client.post(url, json=payload, headers=self.headers)
                elif method.upper() == "DELETE":
                    resp = await client.request("DELETE", url, json=payload, headers=self.headers)
                else:
                    resp = await client.get(url, headers=self.headers)

                if resp.status_code in (200, 201):
                    return resp.json()

                if resp.status_code == 401:
                    masked = self._mask_token()
                    logger.error(
                        f"❌ [WPP API 401 Unauthorized] Запрос {endpoint} к {self.base_url} отклонён! "
                        f"Использован токен: '{masked}' (длина: {len(self.api_key)}). "
                        f"Убедитесь, что WPP_API_KEY в .env или wpp_token в таблице servers совпадает с токеном WPP панели! "
                        f"Ответ WPP: {resp.text}"
                    )
                    return {"ok": False, "status_code": 401, "error": f"Unauthorized: {resp.text}"}

                logger.warning(f"WPP API ({endpoint}) HTTP {resp.status_code}: {resp.text[:200]}")
                return {"ok": False, "status_code": resp.status_code, "error": f"HTTP {resp.status_code}: {resp.text[:100]}"}
        except Exception as e:
            logger.warning(f"WPP API error ({endpoint}) at {self.base_url}: {e}")
            return {"ok": False, "error": str(e)}

    async def create_profile_async(self, protocol: str, name: str) -> dict:
        return await self._request_async("/profiles/create", method="POST", payload={"protocol": protocol, "name": name})

    async def create_client_bundle_async(self, user_tg_id: int, key_id: int = 0, protocols: list = None) -> dict:
        """
        Асинхронное создание профилей на WPP-ноде.
        По умолчанию пробует vless. Hysteria2 запрашивается только если указан явно
        или если vless успешно создан. При ошибке одного протокола второй не ломается.
        """
        configs = []
        profile_ids = []
        profiles = []
        name = f"user_{user_tg_id}_k{key_id}" if key_id else f"user_{user_tg_id}"
        protos = protocols or ["vless"]

        for proto in protos:
            res = await self.create_profile_async(proto, name)
            if res.get("ok") and "profile" in res:
                prof = res["profile"]
                link = prof.get("link")
                pid = str(prof.get("id") or "")
                if link:
                    configs.append(link)
                if pid:
                    profile_ids.append(pid)
                    profiles.append({"protocol": proto, "id": pid, "link": link})
            else:
                logger.debug(f"WPP protocol {proto} not created for {name}: {res.get('error')}")
            if len(protos) > 1:
                await asyncio.sleep(0.5)

        return {
            "configs": configs,
            "profile_ids": profile_ids,
            "profiles": profiles,
            "name": name
        }

    async def delete_profile_async(self, profile_id: str) -> bool:
        if not profile_id:
            return False
        res = await self._request_async("/profiles/delete", method="POST", payload={"id": profile_id})
        return bool(res.get("ok", False))

    async def get_profile_traffic_async(self, profile_id: str) -> dict:
        """Получение статистики профиля (up_bytes, down_bytes)"""
        if not profile_id:
            return {"up": 0, "down": 0, "total": 0}
        res = await self._request_async(f"/profiles/{profile_id}/stats", method="GET")
        if res.get("ok") and "stats" in res:
            st = res["stats"]
            up = int(st.get("upload", st.get("up", 0)))
            down = int(st.get("download", st.get("down", 0)))
            return {"up": up, "down": down, "total": up + down}
        return {"up": 0, "down": 0, "total": 0}

    async def list_profiles_async(self) -> list:
        res = await self._request_async("/profiles", method="GET")
        return res.get("profiles", []) if res.get("ok") else []

    # --- Синхронные обертки для обратной совместимости ---
    def _run_sync(self, coro):
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    return pool.submit(asyncio.run, coro).result()
            return loop.run_until_complete(coro)
        except RuntimeError:
            return asyncio.run(coro)

    def create_client_bundle(self, user_tg_id: int):
        return self._run_sync(self.create_client_bundle_async(user_tg_id))

    def delete_profile(self, profile_id: str):
        return self._run_sync(self.delete_profile_async(profile_id))

    def list_profiles(self):
        return self._run_sync(self.list_profiles_async())
