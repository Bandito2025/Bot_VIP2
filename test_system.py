#!/usr/bin/env python3
"""
Комплексный автономный смоук-тест системы Bot_VIP:
1. Целостность SQLite БД (таблицы, колонки, индексы)
2. Матрица тарифов и изоляция групп (test_tariff_isolation)
3. Продление ключей и неизменность sub_id (test_key_extension)
4. Генерация ссылок 3X-UI (извлечение pbk из realitySettings)
5. Клиент WPP и авторизация (разрешение токена, Bearer)
6. Агрегатор подписок (build_subscription_payload)
7. Автоматическая блокировка по сроку и лимиту трафика (test_auto_expiration_and_limits)
8. Параллельная нагрузка SQLite (test_sqlite_concurrency)
9. Регистрация Telegram-хэндлеров (test_bot_handlers_integrity)
10. Фоновые задачи планировщика (APScheduler / JobQueue)
11. Режим --live (проверка реальных серверов из БД)
"""

import os
import sys
import json
import asyncio
import sqlite3
import logging
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("TestSystem")

from bot import (
    auto_migrate_db,
    DB_PATH,
    get_servers_for_group,
    create_or_extend_user_key,
    sync_traffic_job,
    sync_server_nodes_job,
    expire_vips_job,
    daily_backup_job,
    check_expiry_reminders_job,
    poll_pending_invoices_job,
    normalize_sub_base_url,
    TOKEN,
    callbacks_handler,
    start,
    admin_cmd,
    vip_cmd,
    ban_cmd,
    precheckout_callback,
    successful_payment_callback,
    handle_message
)
from panel_manager import MultiPanelManager, ProvisionResult, generate_3xui_link, XUIAdapter
from wpp_client import WPPClient, resolve_wpp_credentials
from sub_server import build_subscription_payload
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    PreCheckoutQueryHandler,
    MessageHandler,
    filters
)

passed_tests = 0
failed_tests = 0

def report_step(name: str, passed: bool, details: str = ""):
    global passed_tests, failed_tests
    if passed:
        passed_tests += 1
        print(f"  ✅ [PASS] {name}" + (f" -> {details}" if details else ""))
    else:
        failed_tests += 1
        print(f"  ❌ [FAIL] {name}" + (f" -> {details}" if details else ""))


# ------------------------------------------------------------------------------
# 1. Тест целостности SQLite БД
# ------------------------------------------------------------------------------
async def test_db_integrity():
    print("\n🔍 1. Проверка целостности базы данных SQLite...")
    auto_migrate_db(DB_PATH)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        
        tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        needed_tables = ["user_keys", "servers", "tariffs", "user_key_nodes", "users", "settings"]
        missing_tables = [t for t in needed_tables if t not in tables]
        report_step("Наличие ключевых таблиц", len(missing_tables) == 0, f"Отсутствуют: {missing_tables}" if missing_tables else "Все таблицы на месте")

        cur.execute("PRAGMA table_info(user_key_nodes)")
        ukn_cols = {r["name"] for r in cur.fetchall()}
        needed_ukn = {"id", "key_id", "server_id", "panel_type", "remote_id", "remote_email", "up_bytes", "down_bytes", "is_active"}
        report_step("Схема user_key_nodes", needed_ukn.issubset(ukn_cols), f"Колонки: {list(ukn_cols)}")

        cur.execute("PRAGMA table_info(servers)")
        srv_cols = {r["name"] for r in cur.fetchall()}
        needed_srv = {"id", "name", "host", "panel_type", "group_name", "wpp_token", "wpp_url"}
        report_step("Схема servers (наличие wpp_token, wpp_url, group_name)", needed_srv.issubset(srv_cols), f"Колонки: {list(srv_cols)}")


# ------------------------------------------------------------------------------
# 2. Тест изоляции тарифов (test_tariff_isolation)
# ------------------------------------------------------------------------------
async def test_tariff_isolation():
    print("\n🛡️ 2. Тест изоляции тарифов (test_tariff_isolation)...")
    srv_a_id, srv_b_id = None, None
    tar_a_id, tar_b_id = None, None
    test_iso_user = 99991111

    try:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            # Создаем тестовый сервер A для группы "Group_A"
            cur.execute("""
                INSERT INTO servers (name, host, panel_type, panel_url, panel_user, panel_pass, is_active, group_name)
                VALUES ('Тест Сервер A', 'node-a.vpn.test:443', '3xui', 'http://127.0.0.1:9111', 'adm', 'pwd', 1, 'Group_A')
            """)
            srv_a_id = cur.lastrowid

            # Создаем тестовый сервер B для группы "Group_B"
            cur.execute("""
                INSERT INTO servers (name, host, panel_type, panel_url, panel_user, panel_pass, is_active, group_name)
                VALUES ('Тест Сервер B', 'node-b.vpn.test:443', '3xui', 'http://127.0.0.1:9112', 'adm', 'pwd', 1, 'Group_B')
            """)
            srv_b_id = cur.lastrowid

            # Создаем тариф для Group_A
            cur.execute("""
                INSERT INTO tariffs (group_name, name, days, price_rub, price_stars, is_active)
                VALUES ('Group_A', 'Тариф Группа А', 30, 200, 100, 1)
            """)
            tar_a_id = cur.lastrowid

            # Создаем тариф для Group_B
            cur.execute("""
                INSERT INTO tariffs (group_name, name, days, price_rub, price_stars, is_active)
                VALUES ('Group_B', 'Тариф Группа Б', 30, 200, 100, 1)
            """)
            tar_b_id = cur.lastrowid
            conn.commit()

        # 2a. Проверка функции get_servers_for_group
        srvs_a = get_servers_for_group("Group_A")
        srv_a_ids = [s[0] for s in srvs_a]
        report_step("Серверы Group_A содержат Node A и не содержат Node B", (srv_a_id in srv_a_ids) and (srv_b_id not in srv_a_ids))

        srvs_b = get_servers_for_group("Group_B")
        srv_b_ids = [s[0] for s in srvs_b]
        report_step("Серверы Group_B содержат Node B и не содержат Node A", (srv_b_id in srv_b_ids) and (srv_a_id not in srv_b_ids))

        # 2b. Выпуск ключа по тарифу Group_A
        k_id, until, sub_url, cfg = await create_or_extend_user_key(test_iso_user, days=30, create_new=True, tariff_id=tar_a_id)
        
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            nodes = conn.execute("SELECT server_id FROM user_key_nodes WHERE key_id = ?", (k_id,)).fetchall()
            assigned_srv_ids = {n["server_id"] for n in nodes}

        has_a = (srv_a_id in assigned_srv_ids)
        has_b = (srv_b_id in assigned_srv_ids)
        report_step("Ключ по тарифу Group_A не получил чужой сервер Node B", has_a and not has_b, f"Привязаны ноды: {assigned_srv_ids}")

    finally:
        # Очистка
        with sqlite3.connect(DB_PATH) as conn:
            if srv_a_id: conn.execute("DELETE FROM servers WHERE id = ?", (srv_a_id,))
            if srv_b_id: conn.execute("DELETE FROM servers WHERE id = ?", (srv_b_id,))
            if tar_a_id: conn.execute("DELETE FROM tariffs WHERE id = ?", (tar_a_id,))
            if tar_b_id: conn.execute("DELETE FROM tariffs WHERE id = ?", (tar_b_id,))
            conn.execute("DELETE FROM user_keys WHERE user_id = ?", (test_iso_user,))
            conn.execute("DELETE FROM user_key_nodes WHERE key_id IN (SELECT id FROM user_keys WHERE user_id = ?)", (test_iso_user,))
            conn.execute("DELETE FROM users WHERE user_id = ?", (test_iso_user,))
            conn.commit()


# ------------------------------------------------------------------------------
# 3. Тест продления ключа (test_key_extension)
# ------------------------------------------------------------------------------
async def test_key_extension():
    print("\n⏳ 3. Тест продления ключа (test_key_extension)...")
    test_ext_user = 99992222
    try:
        # 1. Создаем ключ на 10 дней
        k_id1, until1, sub_url1, _ = await create_or_extend_user_key(test_ext_user, days=10, create_new=True)
        dt1 = datetime.strptime(until1, "%Y-%m-%d %H:%M:%S")

        # 2. Продлеваем этот же ключ на 15 дней
        k_id2, until2, sub_url2, _ = await create_or_extend_user_key(test_ext_user, days=15, create_new=False, key_id=k_id1)
        dt2 = datetime.strptime(until2, "%Y-%m-%d %H:%M:%S")

        diff_days = round((dt2 - dt1).total_seconds() / 86400)
        same_id = (k_id1 == k_id2)
        same_sub = (sub_url1 == sub_url2)
        days_ok = (diff_days == 15)

        report_step("Идентификатор ключа и sub_id не изменились при продлении", same_id and same_sub, f"ID: {k_id1}, Sub: {sub_url1}")
        report_step("Срок действия увеличился ровно на 15 дней", days_ok, f"Было: {until1} -> Стало: {until2} (+{diff_days} дн.)")
    finally:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("DELETE FROM user_keys WHERE user_id = ?", (test_ext_user,))
            conn.execute("DELETE FROM users WHERE user_id = ?", (test_ext_user,))
            conn.commit()


# ------------------------------------------------------------------------------
# 4. Тест генерации ссылок 3X-UI и извлечения Reality pbk
# ------------------------------------------------------------------------------
async def test_reality_link_generation():
    print("\n🔑 4. Проверка генерации ссылок 3X-UI и Reality pbk...")
    sample_inbound = {
        "id": 1,
        "protocol": "vless",
        "port": 443,
        "remark": "Тест Reality",
        "enable": True,
        "streamSettings": json.dumps({
            "network": "tcp",
            "security": "reality",
            "realitySettings": {
                "serverNames": ["yahoo.com"],
                "shortIds": ["0123456789abcdef"],
                "settings": {
                    "publicKey": "aBcDeFgHiJkLmNoPqRsTuVwXyZ1234567890=",
                    "fingerprint": "chrome",
                    "spiderX": "/assets"
                }
            }
        })
    }
    client_uuid = "11111111-2222-3333-4444-555555555555"
    host = "test.server.online"
    link = generate_3xui_link(sample_inbound, client_uuid, host, country_flag="🇩🇪")
    
    has_pbk = "pbk=aBcDeFgHiJkLmNoPqRsTuVwXyZ1234567890=" in link
    has_sni = "sni=yahoo.com" in link
    has_sid = "sid=0123456789abcdef" in link
    has_flow = "flow=xtls-rprx-vision" in link
    not_empty_pbk = "pbk=&" not in link and not link.endswith("pbk=")
    
    report_step("Извлечение Reality pbk, sni, sid, flow из вложенных streamSettings", has_pbk and has_sni and has_sid and has_flow and not_empty_pbk, f"Ссылка: {link[:80]}...")


# ------------------------------------------------------------------------------
# 5. Проверка клиента WPP и авторизации
# ------------------------------------------------------------------------------
async def test_wpp_client():
    print("\n🌐 5. Проверка клиента WPP и авторизации...")
    test_srv = {
        "id": 1,
        "name": "Тест",
        "panel_type": "both",
        "panel_pass": "3XUI_PASSWORD_SECRET",
        "wpp_token": "CUSTOM_WPP_TOKEN_12345",
        "wpp_url": "http://127.0.0.1:8090"
    }
    url, token = resolve_wpp_credentials(server=test_srv)
    report_step("Изоляция пароля 3X-UI от токена WPP при panel_type='both'", token == "CUSTOM_WPP_TOKEN_12345", f"Токен: {token}")

    wpp = WPPClient(server=test_srv)
    auth_header = wpp.headers.get("Authorization", "")
    report_step("Формирование заголовка Authorization: Bearer", auth_header == "Bearer CUSTOM_WPP_TOKEN_12345", f"Header: {auth_header}")

    stats_res = await wpp.get_profile_traffic_async("fake_profile_id")
    report_step("Отказоустойчивость WPP API при недоступности", isinstance(stats_res, dict) and stats_res.get("total") == 0)


# ------------------------------------------------------------------------------
# 6. Проверка агрегатора подписки (/sub/{sub_id})
# ------------------------------------------------------------------------------
async def test_subscription_aggregator():
    print("\n📦 6. Проверка агрегатора подписки (/sub/{sub_id})...")
    test_uid = 99993333
    try:
        k_id, until_str, sub_url, combined = await create_or_extend_user_key(test_uid, days=15, create_new=True)
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            k_row = conn.execute("SELECT sub_id FROM user_keys WHERE id = ?", (k_id,)).fetchone()
            sub_id = k_row["sub_id"]

        # VPN клиент
        st_vpn, h_vpn, b_vpn = build_subscription_payload(
            sub_id,
            headers={"User-Agent": "Happ/3.2.0 (v2ray)"},
            client_ip="127.0.0.1",
            db_path=DB_PATH
        )
        report_step("Формирование Base64 подписки для VPN-приложений", st_vpn == 200 and "Subscription-Userinfo" in h_vpn and "Profile-Title" in h_vpn)

        # Веб-браузер
        st_web, h_web, b_web = build_subscription_payload(
            sub_id,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"},
            client_ip="127.0.0.1",
            db_path=DB_PATH
        )
        report_step("Отображение HTML веб-дашборда для браузера", st_web == 200 and "text/html" in h_web.get("Content-Type", "") and len(b_web) > 1000)
    finally:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("DELETE FROM user_keys WHERE user_id = ?", (test_uid,))
            conn.execute("DELETE FROM users WHERE user_id = ?", (test_uid,))
            conn.commit()


# ------------------------------------------------------------------------------
# 7. Тест блокировки по трафику и сроку (test_auto_expiration_and_limits)
# ------------------------------------------------------------------------------
async def test_auto_expiration_and_limits():
    print("\n⛔ 7. Тест блокировки по сроку и лимиту трафика (test_auto_expiration_and_limits)...")
    class MockBot:
        async def send_message(self, *args, **kwargs): pass
    class MockContext:
        def __init__(self): self.bot = MockBot()
    ctx = MockContext()

    user_exp = 99994441
    user_lim = 99994442
    try:
        # 7a. Истечение по сроку
        yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO user_keys (user_id, key_name, client_uuid, sub_id, server_id, location_name, traffic_bytes, limit_gb, expires_at, config_text, is_active, warned_24h, warned_3h, created_at)
                VALUES (?, 'ExpKey', 'uuid-exp', 'sub-exp', 0, 'Локация', 0, 0, ?, 'vless://test', 1, 0, 0, ?)
            """, (user_exp, yesterday_str, yesterday_str))
            conn.commit()

        await expire_vips_job(ctx)

        with sqlite3.connect(DB_PATH) as conn:
            is_active_exp = conn.execute("SELECT is_active FROM user_keys WHERE user_id = ?", (user_exp,)).fetchone()[0]
        report_step("Деактивация просроченного ключа (expire_vips_job)", is_active_exp == 0, f"is_active = {is_active_exp}")

        # 7b. Отсечка по лимиту трафика
        future_str = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO user_keys (user_id, key_name, client_uuid, sub_id, server_id, location_name, traffic_bytes, limit_gb, expires_at, config_text, is_active, warned_24h, warned_3h, created_at)
                VALUES (?, 'LimKey', 'uuid-lim', 'sub-lim', 0, 'Локация', 0, 1.0, ?, 'vless://test', 1, 0, 0, ?)
            """, (user_lim, future_str, future_str))
            lim_key_id = cur.lastrowid
            # Вставляем ноду с расходом 2 ГБ при лимите 1 ГБ
            cur.execute("""
                INSERT INTO user_key_nodes (key_id, server_id, panel_type, remote_id, remote_email, up_bytes, down_bytes, is_active)
                VALUES (?, 99999, '3xui', 'uuid-lim', 'email', ?, ?, 1)
            """, (lim_key_id, 1024**3, 1024**3))
            conn.commit()

        await sync_traffic_job(ctx)

        with sqlite3.connect(DB_PATH) as conn:
            is_active_lim = conn.execute("SELECT is_active FROM user_keys WHERE id = ?", (lim_key_id,)).fetchone()[0]
        report_step("Отключение ключа при превышении лимита трафика (sync_traffic_job)", is_active_lim == 0, f"is_active = {is_active_lim} (расход: 2 GB, лимит: 1 GB)")

    finally:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("DELETE FROM user_keys WHERE user_id IN (?, ?)", (user_exp, user_lim))
            conn.execute("DELETE FROM user_key_nodes WHERE remote_id IN ('uuid-exp', 'uuid-lim')")
            conn.execute("DELETE FROM users WHERE user_id IN (?, ?)", (user_exp, user_lim))
            conn.commit()


# ------------------------------------------------------------------------------
# 8. Тест параллельной нагрузки SQLite (test_sqlite_concurrency)
# ------------------------------------------------------------------------------
async def test_sqlite_concurrency():
    print("\n⚡ 8. Тест параллельной нагрузки SQLite (test_sqlite_concurrency)...")
    test_uids = [99995000 + i for i in range(5)]
    sub_ids = []
    k_ids = []
    try:
        # Создаем 5 ключей
        for uid in test_uids:
            k_id, _, _, _ = await create_or_extend_user_key(uid, days=30, create_new=True)
            k_ids.append(k_id)
            with sqlite3.connect(DB_PATH) as conn:
                sub = conn.execute("SELECT sub_id FROM user_keys WHERE id = ?", (k_id,)).fetchone()[0]
                sub_ids.append(sub)

        # 20 одновременных задач: 10 чтений подписки + 10 обновлений трафика
        async def read_task(sub_id):
            return build_subscription_payload(sub_id, headers={"User-Agent": "Happ"}, db_path=DB_PATH)

        async def write_task(k_id, amount):
            with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
                conn.execute("UPDATE user_keys SET traffic_bytes = traffic_bytes + ? WHERE id = ?", (amount, k_id))
                conn.commit()
            return True

        tasks = []
        for i in range(10):
            tasks.append(read_task(sub_ids[i % len(sub_ids)]))
            tasks.append(write_task(k_ids[i % len(k_ids)], (i + 1) * 1024))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        errors = [r for r in results if isinstance(r, Exception)]
        report_step(
            "20 одновременных транзакций без блокировки базы ('database is locked')",
            len(errors) == 0,
            f"Успешно: {len(results) - len(errors)}/20, Ошибок: {len(errors)}"
        )
    finally:
        with sqlite3.connect(DB_PATH) as conn:
            for uid in test_uids:
                conn.execute("DELETE FROM user_keys WHERE user_id = ?", (uid,))
                conn.execute("DELETE FROM users WHERE user_id = ?", (uid,))
            conn.commit()


# ------------------------------------------------------------------------------
# 9. Тест регистрации Telegram-хэндлеров (test_bot_handlers_integrity)
# ------------------------------------------------------------------------------
async def test_bot_handlers_integrity():
    print("\n🤖 9. Тест регистрации Telegram-хэндлеров (test_bot_handlers_integrity)...")
    try:
        app = ApplicationBuilder().token(TOKEN).build()
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("admin", admin_cmd))
        app.add_handler(CommandHandler("vip", vip_cmd))
        app.add_handler(CommandHandler("ban", ban_cmd))
        app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
        app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))
        app.add_handler(CallbackQueryHandler(callbacks_handler))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

        total_handlers = sum(len(h_list) for h_list in app.handlers.values())
        handlers_ok = (total_handlers >= 8)
        report_step("Инициализация и регистрация хэндлеров Telegram Application", handlers_ok, f"Зарегистрировано обработчиков: {total_handlers}")

        # Проверка JobQueue
        jq_ok = app.job_queue is not None
        report_step("Наличие и доступность JobQueue в Application", jq_ok)
    except Exception as e:
        report_step("Инициализация хэндлеров Telegram Application", False, str(e))


# ------------------------------------------------------------------------------
# 10. Проверка фоновых задач планировщика
# ------------------------------------------------------------------------------
async def test_background_jobs():
    print("\n⏰ 10. Проверка фоновых задач планировщика...")
    class MockBot:
        async def send_message(self, *args, **kwargs): pass
        async def send_document(self, *args, **kwargs): pass
    class MockContext:
        def __init__(self): self.bot = MockBot()
    ctx = MockContext()

    try:
        await sync_traffic_job(ctx)
        report_step("Выполнение sync_traffic_job (синхронизация трафика)", True)
    except Exception as e:
        report_step("Выполнение sync_traffic_job", False, str(e))

    try:
        await expire_vips_job(ctx)
        report_step("Выполнение expire_vips_job (деактивация истекших)", True)
    except Exception as e:
        report_step("Выполнение expire_vips_job", False, str(e))

    try:
        await check_expiry_reminders_job(ctx)
        report_step("Выполнение check_expiry_reminders_job (напоминания)", True)
    except Exception as e:
        report_step("Выполнение check_expiry_reminders_job", False, str(e))

    try:
        await poll_pending_invoices_job(ctx)
        report_step("Выполнение poll_pending_invoices_job (опрос счетов)", True)
    except Exception as e:
        report_step("Выполнение poll_pending_invoices_job", False, str(e))

    try:
        await sync_server_nodes_job(ctx)
        report_step("Выполнение 6-й задачи sync_server_nodes_job (синхронизация нод 10 мин)", True)
    except Exception as e:
        report_step("Выполнение sync_server_nodes_job", False, str(e))


# ------------------------------------------------------------------------------
# 11. Проверка нормализации SUB_BASE_URL (порт 8443, sslip.io для IP)
# ------------------------------------------------------------------------------
async def test_url_normalization():
    print("\n🌐 11. Проверка нормализации SUB_BASE_URL (порт 8443, sslip.io)...")
    cases = [
        ("177.202.68.108", "https://177-202-68-108.sslip.io:8443"),
        ("http://177.202.68.108", "https://177-202-68-108.sslip.io:8443"),
        ("sub.svoii-vpn.online", "https://sub.svoii-vpn.online:8443"),
        ("https://sub.svoii-vpn.online", "https://sub.svoii-vpn.online:8443"),
        ("https://sub.svoii-vpn.online:8443", "https://sub.svoii-vpn.online:8443"),
        ("127.0.0.1", "127.0.0.1"),
        ("localhost", "localhost"),
    ]
    all_ok = True
    for raw, expected in cases:
        got = normalize_sub_base_url(raw)
        if got != expected:
            all_ok = False
            report_step(f"Нормализация URL '{raw}'", False, f"Ожидалось '{expected}', получено '{got}'")
    if all_ok:
        report_step("Нормализация всех форматов SUB_BASE_URL (IPv4 -> sslip.io:8443, домен -> :8443)", True)


# ------------------------------------------------------------------------------
# 12. Режим --live (проверка связи с реальными нодами)
# ------------------------------------------------------------------------------
async def test_live_panels():
    print("\n🌐 12. Режим --live: Проверка связи с реальными серверами...")
    if "--live" not in sys.argv:
        print("  ℹ️ [INFO] Режим --live пропущен (запустите 'python test_system.py --live' для проверки связи с реальными серверами).")
        return

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        servers = conn.execute("SELECT * FROM servers WHERE is_active = 1").fetchall()

    if not servers:
        print("  ⚠️ [WARN] В базе данных нет активных серверов для проверки.")
        return

    for srv in servers:
        s_id = srv["id"]
        s_name = srv["name"]
        p_type = str(srv["panel_type"] or "3xui").lower()
        p_url = srv["panel_url"] or ""
        print(f"\n  📡 Проверка ноды #{s_id} [{s_name}] (Тип: {p_type}):")

        # Проверка 3X-UI
        if p_type in ("3xui", "3x-ui", "both"):
            if not p_url:
                report_step(f"3X-UI Нода #{s_id}", False, "panel_url не задан")
            else:
                try:
                    adapter = XUIAdapter(p_url, srv["panel_user"], srv["panel_pass"], timeout=4.0)
                    inbs = await adapter.get_inbounds()
                    if inbs:
                        report_step(f"3X-UI Нода #{s_id} ({p_url})", True, f"Авторизован, инбаундов: {len(inbs)}")
                    else:
                        report_step(f"3X-UI Нода #{s_id} ({p_url})", False, "Авторизация не удалась или список пуст")
                except Exception as e:
                    report_step(f"3X-UI Нода #{s_id} ({p_url})", False, f"Ошибка подключения: {e}")

        # Проверка WPP
        if p_type in ("wpp", "web_panel_proxy", "both"):
            try:
                wpp = WPPClient(server=dict(srv), timeout=4.0)
                profiles = await wpp.list_profiles_async()
                masked = wpp._mask_token()
                report_step(f"WPP Нода #{s_id} ({wpp.base_url})", True, f"Токен {masked} принят, профилей: {len(profiles)}")
            except Exception as e:
                report_step(f"WPP Нода #{s_id}", False, f"Ошибка WPP: {e}")


# ------------------------------------------------------------------------------
# ------------------------------------------------------------------------------
# 12. ТЕСТ ОГРАНИЧЕНИЯ ТРИАЛА И ПОДПИСКИ НА КАНАЛ
# ------------------------------------------------------------------------------
async def test_trial_protection():
    print("\n🎁 12. Тест защиты триала и подписки на новостной канал...")
    try:
        from bot import is_trial_used, get_user_trial_lock, verify_trial_channel_subscription
        
        # 1. Проверка для пользователя без ключей и trial_used=0
        test_uid = 99998881
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT OR REPLACE INTO users (user_id, trial_used) VALUES (?, 0)", (test_uid,))
            conn.execute("DELETE FROM user_keys WHERE user_id = ?", (test_uid,))
            conn.commit()
            
        used_before = is_trial_used(test_uid)
        report_step("Новый пользователь: триал доступен", not used_before, f"is_trial_used={used_before}")

        # 2. Проверка после установки trial_used = 1
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("UPDATE users SET trial_used = 1 WHERE user_id = ?", (test_uid,))
            conn.commit()
        used_after = is_trial_used(test_uid)
        report_step("Пользователь с trial_used=1: триал заблокирован", used_after, f"is_trial_used={used_after}")

        # 3. Проверка для пользователя с существующим ключом (даже если trial_used=0)
        test_uid_2 = 99998882
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT OR REPLACE INTO users (user_id, trial_used) VALUES (?, 0)", (test_uid_2,))
            conn.execute("INSERT INTO user_keys (user_id, server_id, client_uuid, is_active) VALUES (?, 1, 'mock-uuid', 1)", (test_uid_2,))
            conn.commit()
        used_with_key = is_trial_used(test_uid_2)
        report_step("Пользователь с существующим ключом: триал заблокирован", used_with_key, f"is_trial_used={used_with_key}")

        # 4. Проверка per-user lock
        lock1 = get_user_trial_lock(test_uid)
        lock2 = get_user_trial_lock(test_uid)
        lock3 = get_user_trial_lock(test_uid_2)
        report_step("Изоляция asyncio.Lock на пользователя", (lock1 is lock2) and (lock1 is not lock3), "Уникальный Lock для каждого user_id")

        # Cleanup
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("DELETE FROM users WHERE user_id IN (?, ?)", (test_uid, test_uid_2))
            conn.execute("DELETE FROM user_keys WHERE user_id IN (?, ?)", (test_uid, test_uid_2))
            conn.commit()

    except Exception as e:
        report_step("Тест защиты триала", False, f"Исключение: {e}")


# ------------------------------------------------------------------------------
# 13. ТЕСТ ВИЗАРДА СЕРВЕРОВ WPP И ГИБРИДНОГО РЕЖИМА
# ------------------------------------------------------------------------------
async def test_server_wizard_wpp_hybrid():
    print("\n⚡ 13. Тест визарда серверов WPP и гибридного режима...")
    try:
        import server_wizard
        
        # 1. Сохранение WPP-ноды
        ok_wpp = server_wizard.save_server_record(
            name="Test WPP Node",
            host="123.45.67.89:8000",
            panel_type="wpp",
            wpp_url="http://123.45.67.89:8000",
            wpp_token="TEST_BEARER_TOKEN",
            group_name="VIP"
        )
        report_step("Сохранение сервера с panel_type='wpp'", ok_wpp)

        # 2. Сохранение гибридной ноды (3X-UI + WPP)
        ok_both = server_wizard.save_server_record(
            name="Test Both Node",
            host="123.45.67.90:2053",
            panel_url="https://123.45.67.90:2053/secret/",
            login="api_token",
            password="XUI_SECRET_TOKEN",
            panel_type="both",
            wpp_url="http://123.45.67.90:8000",
            wpp_token="WPP_SECRET_TOKEN",
            group_name="Стандарт"
        )
        report_step("Сохранение сервера с panel_type='both'", ok_both)

        # 3. Проверка сохранённых данных в базе
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            row_wpp = conn.execute("SELECT * FROM servers WHERE name = 'Test WPP Node'").fetchone()
            row_both = conn.execute("SELECT * FROM servers WHERE name = 'Test Both Node'").fetchone()

        wpp_valid = bool(row_wpp and row_wpp["panel_type"] == "wpp" and row_wpp["wpp_token"] == "TEST_BEARER_TOKEN" and row_wpp["group_name"] == "VIP")
        both_valid = bool(row_both and row_both["panel_type"] == "both" and row_both["panel_url"].startswith("https://") and row_both["wpp_token"] == "WPP_SECRET_TOKEN")
        report_step("Корректность реквизитов WPP и 3X-UI в БД", wpp_valid and both_valid, f"WPP ok: {bool(row_wpp)}, Both ok: {bool(row_both)}")

        # 4. Проверка тестовой функции WPP (валидация пустого URL)
        ok_empty, msg_empty, _ = server_wizard.test_wpp_connection_sync("", "")
        report_step("Проверка валидации пустого URL WPP", not ok_empty, msg_empty)

        # Cleanup
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("DELETE FROM servers WHERE name IN ('Test WPP Node', 'Test Both Node')")
            conn.commit()

    except Exception as e:
        report_step("Тест визарда WPP и гибрида", False, f"Исключение: {e}")


# ------------------------------------------------------------------------------
# 14. ТЕСТ ДИНАМИЧЕСКИХ ТАРИФОВ И СТАРТОВОГО ЧЕКЛИСТА
# ------------------------------------------------------------------------------
async def test_dynamic_tariffs_and_start_checklist():
    print("\n📋 14. Тест динамических тарифов и стартового чеклиста /start...")
    try:
        from bot import build_tariffs_block, build_custom_start_content
        
        # 1. Проверка динамического блока тарифов
        t_block = build_tariffs_block()
        report_step("Генерация блока тарифов build_tariffs_block()", bool(t_block) and "Тарифы" in t_block, t_block[:80])

        # 2. Проверка вызова build_custom_start_content для обычного пользователя
        caption, kb = build_custom_start_content(user_id=12345678)
        report_step("Формирование контента /start для клиента", bool(caption) and bool(kb), f"Кнопок: {len(kb.inline_keyboard)}")

        # 3. Проверка скрытия кнопки триала при trial_used=1
        test_uid = 99997771
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT OR REPLACE INTO users (user_id, trial_used) VALUES (?, 1)", (test_uid,))
            conn.commit()
        caption_u, kb_u = build_custom_start_content(user_id=test_uid)
        trial_btn_present = any(
            any(btn.callback_data == "claim_trial" for btn in row)
            for row in kb_u.inline_keyboard
        )
        report_step("Кнопка триала скрыта для пользователя с trial_used=1", not trial_btn_present, "Кнопка 'claim_trial' отсутствует")

        # Cleanup
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("DELETE FROM users WHERE user_id = ?", (test_uid,))
            conn.commit()

    except Exception as e:
        report_step("Тест динамических тарифов и чеклиста", False, f"Исключение: {e}")

# MAIN RUNNER
# ------------------------------------------------------------------------------
async def main():
    print("=" * 70)
    print("🚀 ЗАПУСК КОМПЛЕКСНОГО СМОУК-ТЕСТА СИСТЕМЫ (Bot_VIP)")
    print("=" * 70)

    await test_db_integrity()
    await test_tariff_isolation()
    await test_key_extension()
    await test_reality_link_generation()
    await test_wpp_client()
    await test_subscription_aggregator()
    await test_auto_expiration_and_limits()
    await test_sqlite_concurrency()
    await test_bot_handlers_integrity()
    await test_background_jobs()
    await test_url_normalization()
    await test_trial_protection()
    await test_server_wizard_wpp_hybrid()
    await test_dynamic_tariffs_and_start_checklist()
    await test_live_panels()

    print("\n" + "=" * 70)
    print(f"📊 ИТОГИ ТЕСТИРОВАНИЯ: УСПЕШНО: {passed_tests} | ОШИБОК: {failed_tests}")
    print("=" * 70)

    if failed_tests > 0:
        sys.exit(1)
    else:
        print("🎉 ВСЕ ПРОВЕРКИ УСПЕШНО ПРОЙДЕНЫ!")
        sys.exit(0)

if __name__ == "__main__":
    asyncio.run(main())
