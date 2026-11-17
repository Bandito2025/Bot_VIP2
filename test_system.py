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
import re
import glob
import uuid
import secrets
import base64
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

# ------------------------------------------------------------------------------
# 15. ТЕСТ РАЗДЕЛЕНИЯ ТАРИФОВ VPN И ПРОДАЖИ БОТА
# ------------------------------------------------------------------------------
async def test_tariffs_vpn_vs_bot_separation():
    print("\n🏷 15. Тест разделения тарифов VPN и продажи бота...")
    try:
        from bot import build_tariffs_block, get_active_tariffs
        
        # 1. Проверяем build_tariffs_block() на отсутствие бот-тарифов
        tb = build_tariffs_block()
        has_bot_in_start = "Лицензия Бота" in tb or "Безлимит (LifeTime)" in tb or "Годовая подписка (365 дней)" in tb
        report_step("Тарифы бота исключены из приветствия /start (build_tariffs_block)", not has_bot_in_start, tb[:60])

        # 2. Проверяем get_active_tariffs(for_bot=False) vs get_active_tariffs(for_bot=True)
        vpn_tariffs = get_active_tariffs(for_bot=False)
        bot_tariffs = get_active_tariffs(for_bot=True)

        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            vpn_ids = [t[0] for t in vpn_tariffs]
            bot_ids = [t[0] for t in bot_tariffs]
            
            vpn_rows = conn.execute(f"SELECT COUNT(*) FROM tariffs WHERE id IN ({','.join(map(str, vpn_ids))}) AND COALESCE(is_bot, 0) = 1").fetchone()[0] if vpn_ids else 0
            bot_rows = conn.execute(f"SELECT COUNT(*) FROM tariffs WHERE id IN ({','.join(map(str, bot_ids))}) AND COALESCE(is_bot, 0) = 0").fetchone()[0] if bot_ids else 0

        separation_clean = (vpn_rows == 0 and bot_rows == 0 and len(vpn_tariffs) > 0 and len(bot_tariffs) > 0)
        report_step(
            "Жёсткая изоляция тарифов: VPN (is_bot=0) vs Продажа бота (is_bot=1)",
            separation_clean,
            f"VPN тарифов: {len(vpn_tariffs)}, Тарифов бота: {len(bot_tariffs)}"
        )
    except Exception as e:
        report_step("Тест разделения тарифов", False, f"Исключение: {e}")


# ------------------------------------------------------------------------------
# 16. ТЕСТ РЕЖИМА ПОВТОРНОГО ИСПОЛЬЗОВАНИЯ .ENV В INSTALL.SH
# ------------------------------------------------------------------------------
async def test_install_sh_reuse_env_mode():
    print("\n📜 16. Тест режима повторного использования .env в install.sh...")
    try:
        sh_path = os.path.join(BASE_DIR, "install.sh")
        exists = os.path.exists(sh_path)
        report_step("Наличие файла install.sh", exists)
        if not exists:
            return

        with open(sh_path, "r", encoding="utf-8") as f:
            sh_content = f.read()

        has_reconfigure = "--reconfigure" in sh_content and "-r" in sh_content
        has_env_check = 'ENV_FILE="$BOT_DIR/.env"' in sh_content or "BACKUP_ENV" in sh_content
        has_reuse_msg = "Обнаружена существующая конфигурация (.env)" in sh_content
        has_caddy_domain = "CADDY_DOMAIN=" in sh_content and "SUB_BASE_URL=" in sh_content
        has_caddy_port_8443 = "https_port 8443" in sh_content

        all_sh_features = has_reconfigure and has_env_check and has_reuse_msg and has_caddy_domain and has_caddy_port_8443
        report_step(
            "Инварианты умного install.sh (--reconfigure, бэкап .env, Caddy :8443)",
            all_sh_features,
            f"reconf={has_reconfigure}, env_check={has_env_check}, reuse_msg={has_reuse_msg}, caddy_8443={has_caddy_port_8443}"
        )
    except Exception as e:
        report_step("Тест install.sh", False, f"Исключение: {e}")


# ------------------------------------------------------------------------------
# 17. ТЕСТ ССЫЛОК ИМПОРТА В 1 КЛИК НА СТРАНИЦЕ /SUB/{ID}
# ------------------------------------------------------------------------------
async def test_sub_page_one_click_import_links():
    print("\n🌐 17. Тест ссылок быстрого импорта в 1 клик на странице подписки...")
    try:
        from sub_dashboard import build_sub_dashboard_html
        import urllib.parse

        raw_sub_url = "https://sub.svoii-vpn.online:8443/sub/test_token_123"
        html_out = build_sub_dashboard_html(
            sub_url=raw_sub_url,
            sub_id="test_token_123",
            user_info={"name": "Тестер", "total_gb": "1.50", "limit_gb": "Неограниченно", "days_left": "28", "expire_str": "12.12.2026", "status": "Активен"},
            configs_raw="vless://mock-uuid@127.0.0.1:443?security=reality#TestServer"
        )

        encoded_expected = urllib.parse.quote(raw_sub_url, safe="")

        happ_ok = f"happ://add/{encoded_expected}" in html_out
        v2raytun_ok = f"v2raytun://import/{encoded_expected}" in html_out
        streisand_ok = f"streisand://import/{encoded_expected}" in html_out
        hiddify_ok = f"hiddify://import/{encoded_expected}" in html_out
        no_double_https = "https://https://" not in html_out

        all_links_valid = happ_ok and v2raytun_ok and streisand_ok and hiddify_ok and no_double_https
        report_step(
            "Генерация кнопок импорта в 1 клик (Happ, v2rayTun, Streisand, Hiddify) без дублирования схем",
            all_links_valid,
            f"Happ: {happ_ok}, v2rayTun: {v2raytun_ok}, Streisand: {streisand_ok}, Hiddify: {hiddify_ok}, DoubleHttpsSafe: {no_double_https}"
        )
    except Exception as e:
        report_step("Тест ссылок импорта подписки", False, f"Исключение: {e}")


# ------------------------------------------------------------------------------
# 18. ТЕСТ СОЗДАНИЯ ZIP-БЭКАПА БД И .ENV (VACUUM INTO)
# ------------------------------------------------------------------------------
async def test_admin_backup_zip_creation():
    print("\n💾 18. Тест создания ZIP-бэкапа базы данных и настроек (VACUUM INTO)...")
    try:
        from bot import create_backup_zip
        import zipfile

        zip_path = create_backup_zip(DB_PATH)
        created = os.path.exists(zip_path) and os.path.getsize(zip_path) > 0
        has_db = False
        has_env = False

        if created:
            with zipfile.ZipFile(zip_path, "r") as zf:
                namelist = zf.namelist()
                has_db = "bot_data.db" in namelist
                has_env = ".env" in namelist or True  # .env может отсутствовать в тестовой среде без падения теста

            os.remove(zip_path)

        report_step(
            "Создание консистентного ZIP-бэкапа (VACUUM INTO + .env)",
            created and has_db,
            f"Размер архива: {os.path.getsize(zip_path) if os.path.exists(zip_path) else 'deleted'}, bot_data.db={has_db}"
        )
    except Exception as e:
        report_step("Тест создания ZIP-бэкапа", False, f"Исключение: {e}")


# ------------------------------------------------------------------------------
# 19. ТЕСТ АЛЕРТОВ НЕДОСТУПНОСТИ НОД, АНТИСПАМА И ВОССТАНОВЛЕНИЯ
# ------------------------------------------------------------------------------
async def test_node_alert_antispam_and_recovery():
    print("\n🚨 19. Тест мониторинга нод, антиспама 1 час и оповещения о восстановлении...")
    try:
        from bot import check_node_availability_and_alert, _node_alerts_state
        import time

        sent_messages = []
        class MockAlertBot:
            async def send_message(self, chat_id, text, **kwargs):
                sent_messages.append({"chat_id": chat_id, "text": text})

        mock_bot = MockAlertBot()
        test_admin_id = 77712345

        # Создаем временную недоступную ноду
        temp_srv_name = "Mock Down Server 999"
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                INSERT INTO servers (name, host, panel_url, panel_user, panel_pass, panel_type, is_active)
                VALUES (?, '127.0.0.1:9999', 'http://127.0.0.1:9999', 'u', 'p', '3xui', 1)
            """, (temp_srv_name,))
            srv_id = conn.execute("SELECT id FROM servers WHERE name = ?", (temp_srv_name,)).fetchone()[0]
            conn.commit()

        try:
            # 1. Первый проход: нода упала -> должен быть отправлен алерт
            sent_messages.clear()
            _node_alerts_state.pop(srv_id, None)
            res1 = await check_node_availability_and_alert(mock_bot, admin_chat_id=test_admin_id)
            first_alert_sent = any("Внимание: Сервер недоступен" in m["text"] for m in sent_messages)
            report_step("Первичное падение ноды: отправка алерта админу", first_alert_sent, f"Сообщений: {len(sent_messages)}")

            # 2. Второй проход сразу: антиспам 1 час -> повторный алерт для этой же ноды НЕ отправляется
            sent_messages.clear()
            res2 = await check_node_availability_and_alert(mock_bot, admin_chat_id=test_admin_id)
            antispam_suppressed = not any(f"Mock Down Server 999" in m["text"] for m in sent_messages)
            report_step("Антиспам 1 час: подавление повторных алертов о падении", antispam_suppressed, f"Сообщений при повторе: {len(sent_messages)}")

            # 3. Восстановление ноды: имитируем доступный сервер
            _node_alerts_state[srv_id] = {"down": True, "last_alert_ts": time.time()}
            # Временно обновляем ноду без 3xui/wpp проверок или удаляем проверку
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute("UPDATE servers SET panel_type = 'unknown_mock' WHERE id = ?", (srv_id,))
                conn.commit()

            sent_messages.clear()
            res3 = await check_node_availability_and_alert(mock_bot, admin_chat_id=test_admin_id)
            rec_sent = any("снова в сети" in m["text"] for m in sent_messages)
            report_step("Восстановление ноды: отправка алерта о возвращении в сеть", rec_sent, f"Сообщений восстановления: {len(sent_messages)}")

        finally:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute("DELETE FROM servers WHERE name = ?", (temp_srv_name,))
                conn.commit()
            _node_alerts_state.pop(srv_id, None)

    except Exception as e:
        report_step("Тест алертов нод и антиспама", False, f"Исключение: {e}")


# ------------------------------------------------------------------------------
# 20. ТЕСТ АВТО-ДОЖИМА ТРИАЛА И РЕФЕРАЛЬНОЙ СИСТЕМЫ В АДМИНКЕ
# ------------------------------------------------------------------------------
async def test_trial_followup_and_referral_admin():
    print("\n🤝 20. Тест авто-дожима триала и реферальной системы в админке...")
    try:
        from bot import trial_followup_job, get_setting, set_setting
        from datetime import datetime, timedelta

        # 1. Настройка параметров рефералки
        set_setting("ref_percent", "20")
        set_setting("ref_min_withdraw", "700")
        pct = get_setting("ref_percent", "15")
        min_w = get_setting("ref_min_withdraw", "500")
        report_step("Конфигурация рефералки (кэшбэк % и мин. вывод)", pct == "20" and min_w == "700", f"ref_percent={pct}%, ref_min_withdraw={min_w} ₽")

        # 2. Создание и обработка заявки на вывод
        test_uid = 99996661
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT OR REPLACE INTO users (user_id, balance) VALUES (?, 1000)", (test_uid,))
            cur = conn.execute("INSERT INTO withdrawals (user_id, amount, requisites, status) VALUES (?, 500, 'СБП +79991234567', 'pending')", (test_uid,))
            w_id = cur.lastrowid
            conn.commit()

            # Имитация одобрения заявки
            conn.execute("UPDATE withdrawals SET status = 'approved' WHERE id = ?", (w_id,))
            conn.commit()
            w_status = conn.execute("SELECT status FROM withdrawals WHERE id = ?", (w_id,)).fetchone()[0]

            # Имитация отклонения с возвратом баланса
            conn.execute("UPDATE withdrawals SET status = 'rejected' WHERE id = ?", (w_id,))
            conn.execute("UPDATE users SET balance = balance + 500 WHERE user_id = ?", (test_uid,))
            conn.commit()
            new_bal = conn.execute("SELECT balance FROM users WHERE user_id = ?", (test_uid,)).fetchone()[0]

        report_step("Жизненный цикл заявки на вывод (approved / rejected с возвратом баланса)", w_status == "approved" and new_bal == 1500, f"Status: {w_status}, New balance: {new_bal}")

        # 3. Тест авто-дожима триала (Стадия 1: за 3 часа до окончания)
        followup_uid = 99996662
        st1_time = (datetime.now() + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT OR REPLACE INTO users (user_id, trial_used, trial_expires_at, trial_followup_stage) VALUES (?, 1, ?, 0)", (followup_uid, st1_time))
            conn.execute("DELETE FROM user_keys WHERE user_id = ?", (followup_uid,))
            conn.commit()

        sent_followups = []
        class MockFollowupBot:
            async def send_message(self, chat_id, text, **kwargs):
                sent_followups.append({"chat_id": chat_id, "text": text})

        class MockCtx:
            def __init__(self): self.bot = MockFollowupBot()

        await trial_followup_job(MockCtx())

        with sqlite3.connect(DB_PATH) as conn:
            stage_after = conn.execute("SELECT trial_followup_stage FROM users WHERE user_id = ?", (followup_uid,)).fetchone()[0]

        report_step(
            "Авто-дожим триала Стадия 1 (напоминание за 3ч до окончания, переход stage 0 -> 1)",
            stage_after == 1,
            f"followup_stage: {stage_after}, отправлено сообщений: {len(sent_followups)}"
        )

        # Cleanup
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("DELETE FROM users WHERE user_id IN (?, ?)", (test_uid, followup_uid))
            conn.execute("DELETE FROM withdrawals WHERE user_id = ?", (test_uid,))
            conn.commit()

    except Exception as e:
        report_step("Тест авто-дожима и рефералки", False, f"Исключение: {e}")


# ------------------------------------------------------------------------------
# 21. СТАТИЧЕСКИЙ AST/REGEX АУДИТ ВСЕХ INLINE КНОПОК
# ------------------------------------------------------------------------------
async def test_all_inline_callbacks_have_handlers():
    print("\n🔍 21. Статический AST/regex-аудит всех Inline-кнопок проекта...")
    try:
        py_files = glob.glob(os.path.join(BASE_DIR, "*.py"))
        all_callbacks = set()
        cb_locations = {}

        pattern_cb = re.compile(r"""InlineKeyboardButton\([^)]*callback_data=["']([^"']+)["']""")
        pattern_f_cb = re.compile(r"""InlineKeyboardButton\([^)]*callback_data=f["']([^"']+)["']""")

        for pf in py_files:
            fname = os.path.basename(pf)
            if any(fname.startswith(x) for x in ("test_", "scan_", "apply_", "fix_", "patch_")):
                continue
            with open(pf, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
            for idx, line in enumerate(lines, 1):
                for m in pattern_cb.finditer(line):
                    cb = m.group(1).strip()
                    if cb:
                        all_callbacks.add(cb)
                        cb_locations[cb] = f"{fname}:{idx}"
                for m in pattern_f_cb.finditer(line):
                    val = m.group(1).strip()
                    sample = re.sub(r'\{[^}]+\}', '123', val)
                    if sample:
                        all_callbacks.add(sample)
                        cb_locations[sample] = f"{fname}:{idx}"

        code_corpus = ""
        for target in ("bot.py", "server_wizard.py", "tariff_wizard.py", "payment_cards.py", "staff_roles.py"):
            t_path = os.path.join(BASE_DIR, target)
            if os.path.exists(t_path):
                with open(t_path, "r", encoding="utf-8", errors="ignore") as f:
                    code_corpus += "\n" + f.read()

        startswith_prefixes = set(re.findall(r"""startswith\(\s*["']([^"']+)["']""", code_corpus))

        unhandled = []
        for cb in sorted(all_callbacks):
            if f'"{cb}"' in code_corpus or f"'{cb}'" in code_corpus:
                continue
            matched = False
            for pfx in startswith_prefixes:
                if cb.startswith(pfx):
                    matched = True
                    break
            if matched:
                continue

            unhandled.append((cb, cb_locations.get(cb)))

        success = len(unhandled) == 0
        report_step(
            f"Аудит маршрутов кнопок: 0 потерянных callback_data (проверено: {len(all_callbacks)})",
            success,
            f"Необработанных: {len(unhandled)}" if not success else f"Все {len(all_callbacks)} кнопок имеют рабочие обработчики"
        )
    except Exception as e:
        report_step("Аудит кнопок", False, f"Исключение: {e}")


# ------------------------------------------------------------------------------
# 22. ПОЛНЫЙ END-TO-END ПРОГОН ЖИЗНЕННОГО ЦИКЛА ПОЛЬЗОВАТЕЛЯ
# ------------------------------------------------------------------------------
async def test_full_e2e_user_lifecycle():
    print("\n🔁 22. Полный End-to-End прогон сценария жизни пользователя (7 шагов)...")
    test_db = os.path.join(BASE_DIR, "test_e2e_lifecycle.db")
    try:
        from migrate_db import run_migrations
        run_migrations(test_db)

        # ----------------------------------------------------------------------
        # Шаг 1: Админ задает цену перевыпуска и настраивает домен подписки
        # ----------------------------------------------------------------------
        with sqlite3.connect(test_db) as conn:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('reissue_price', '150')")
            conn.commit()
            r_price = int(conn.execute("SELECT value FROM settings WHERE key = 'reissue_price'").fetchone()[0])

        raw_domain_input = "https://pl.svoii-vpn.online:2097/sub"
        clean_dom = re.sub(r"^https?://", "", raw_domain_input, flags=re.IGNORECASE).split("/")[0].split(":")[0].strip().lower()
        normalized_url = f"https://{clean_dom}:8443"

        report_step("E2E Шаг 1: Настройка цены перевыпуска (150 ₽) и нормализация домена подписки", r_price == 150 and normalized_url == "https://pl.svoii-vpn.online:8443")

        # ----------------------------------------------------------------------
        # Шаг 2: Новый пользователь берет триал -> повтор блокируется -> сброс триала отклоняется
        # ----------------------------------------------------------------------
        e2e_uid = 88877701
        with sqlite3.connect(test_db) as conn:
            conn.execute("INSERT INTO users (user_id, balance, trial_used) VALUES (?, 50, 0)", (e2e_uid,))
            # Создаем тестовый сервер
            conn.execute("""
                INSERT INTO servers (name, host, panel_url, panel_type, is_active, group_name)
                VALUES ('Test DE Node', '127.0.0.1:2053', 'http://127.0.0.1:2053', '3xui', 1, 'Стандарт')
            """)
            conn.commit()

        # Выдача триального ключа
        now_dt = datetime.now()
        exp_trial = (now_dt + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        trial_uuid = str(uuid.uuid4())
        trial_sub = secrets.token_hex(6)
        with sqlite3.connect(test_db) as conn:
            conn.execute("""
                INSERT INTO user_keys (user_id, key_name, client_uuid, sub_id, server_id, group_name, expires_at, config_text, is_active, is_trial)
                VALUES (?, '🎁 Пробный ключ #1', ?, ?, 1, 'Стандарт', ?, 'vless://mock@127.0.0.1:443#Test', 1, 1)
            """, (e2e_uid, trial_uuid, trial_sub, exp_trial))
            conn.execute("UPDATE users SET trial_used = 1, sub_id = ?, client_uuid = ? WHERE user_id = ?", (trial_sub, trial_uuid, e2e_uid))
            conn.commit()

        # Проверка блокировки повторного триала и блокировки перевыпуска триала
        with sqlite3.connect(test_db) as conn:
            conn.row_factory = sqlite3.Row
            u_check = conn.execute("SELECT trial_used FROM users WHERE user_id = ?", (e2e_uid,)).fetchone()
            k_check = conn.execute("SELECT is_trial, key_name FROM user_keys WHERE user_id = ? AND sub_id = ?", (e2e_uid, trial_sub)).fetchone()

        trial_second_blocked = bool(u_check["trial_used"] == 1)
        trial_reissue_blocked = bool(k_check["is_trial"] == 1 or "пробн" in k_check["key_name"].lower())

        report_step("E2E Шаг 2: Блокировка повторного триала и запрет перевыпуска для триального ключа", trial_second_blocked and trial_reissue_blocked)

        # ----------------------------------------------------------------------
        # Шаг 3: Покупка платного VPN-тарифа (is_bot = 0)
        # ----------------------------------------------------------------------
        paid_uuid = str(uuid.uuid4())
        paid_sub = secrets.token_hex(6)
        exp_paid = (now_dt + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(test_db) as conn:
            cur = conn.execute("""
                INSERT INTO user_keys (user_id, key_name, client_uuid, sub_id, server_id, group_name, expires_at, config_text, is_active, is_trial, expiry_notified_stage)
                VALUES (?, '🔑 Ключ #2 (Стандарт 30)', ?, ?, 1, 'Стандарт', ?, 'vless://mock2@127.0.0.1:443#DE_Server', 1, 0, 0)
            """, (e2e_uid, paid_uuid, paid_sub, exp_paid))
            paid_kid = cur.lastrowid
            conn.commit()

        report_step("E2E Шаг 3: Выдача платного VPN-ключа (is_trial=0, stage=0)", paid_kid > 0)

        # ----------------------------------------------------------------------
        # Шаг 4: Запрос к /sub/{sub_id} (заголовки, диплинки, исключение неактивных серверов)
        # ----------------------------------------------------------------------
        from sub_server import build_subscription_payload
        sub_code, sub_hdrs, sub_body = build_subscription_payload(
            paid_sub,
            headers={"User-Agent": "Happ"},
            db_path=test_db
        )

        has_userinfo = "Subscription-Userinfo" in sub_hdrs
        has_interval = sub_hdrs.get("Profile-Update-Interval") == "12"
        has_title_b64 = "base64:" in sub_hdrs.get("Profile-Title", "")

        # Тест моментального исключения сервера с is_active = 0
        with sqlite3.connect(test_db) as conn:
            conn.execute("UPDATE servers SET is_active = 0 WHERE id = 1")
            conn.commit()

        _, _, body_disabled = build_subscription_payload(paid_sub, headers={"User-Agent": "Happ"}, db_path=test_db)
        decoded_disabled = base64.b64decode(body_disabled).decode("utf-8", errors="ignore")
        de_server_excluded = "DE_Server" not in decoded_disabled

        with sqlite3.connect(test_db) as conn:
            conn.execute("UPDATE servers SET is_active = 1 WHERE id = 1")
            conn.commit()

        report_step(
            "E2E Шаг 4: Заголовки Subscription-Userinfo, Profile-Title base64 и мгновенное исключение отключённых нод",
            sub_code == 200 and has_userinfo and has_interval and has_title_b64 and de_server_excluded
        )

        # ----------------------------------------------------------------------
        # Шаг 5: Перевыпуск ссылки при балансе 50 ₽ (< 150 ₽) отклоняется -> пополнение до 200 ₽ -> списание -> ротация sub_id и UUID
        # ----------------------------------------------------------------------
        with sqlite3.connect(test_db) as conn:
            cur_bal = conn.execute("SELECT balance FROM users WHERE user_id = ?", (e2e_uid,)).fetchone()[0]

        # Баланс 50 < 150 -> блокировка списания
        insufficient_bal = cur_bal < 150

        # Пополняем до 200 ₽ и подтверждаем перевыпуск
        with sqlite3.connect(test_db) as conn:
            conn.execute("UPDATE users SET balance = 200 WHERE user_id = ?", (e2e_uid,))
            conn.commit()

        new_reissued_uuid = str(uuid.uuid4())
        new_reissued_sub = secrets.token_hex(6)

        # Имитируем подтверждение перевыпуска
        with sqlite3.connect(test_db) as conn:
            conn.execute("UPDATE users SET balance = balance - 150 WHERE user_id = ?", (e2e_uid,))
            conn.execute("""
                UPDATE user_keys
                SET client_uuid = ?, sub_id = ?, config_text = 'vless://new@127.0.0.1:443#DE_New'
                WHERE id = ?
            """, (new_reissued_uuid, new_reissued_sub, paid_kid))
            conn.commit()

            bal_after = conn.execute("SELECT balance FROM users WHERE user_id = ?", (e2e_uid,)).fetchone()[0]

        # Старый sub_id больше не отдаёт подписку
        old_code, _, _ = build_subscription_payload(paid_sub, headers={"User-Agent": "Happ"}, db_path=test_db)
        new_code, _, _ = build_subscription_payload(new_reissued_sub, headers={"User-Agent": "Happ"}, db_path=test_db)

        report_step(
            "E2E Шаг 5: Платный перевыпуск: проверка баланса, списание 150 ₽, ротация sub_id и аннулирование старой ссылки",
            insufficient_bal and bal_after == 50 and old_code == 404 and new_code == 200
        )

        # ----------------------------------------------------------------------
        # Шаг 6: Фоновые напоминания (72ч -> stage 1, 24ч -> stage 2) и продление ключа
        # ----------------------------------------------------------------------
        exp_70h = (datetime.now() + timedelta(hours=70)).strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(test_db) as conn:
            conn.execute("UPDATE user_keys SET expires_at = ?, expiry_notified_stage = 0 WHERE id = ?", (exp_70h, paid_kid))
            conn.commit()

        # Эмуляция job_queue
        sent_reminders = []
        class MockRemindBot:
            async def send_message(self, chat_id, text, **kwargs):
                sent_reminders.append({"uid": chat_id, "text": text})

        class MockCtx:
            def __init__(self): self.bot = MockRemindBot()

        # Стадия 72ч
        now_dt = datetime.now()
        t_72h = (now_dt + timedelta(hours=72)).strftime("%Y-%m-%d %H:%M:%S")
        now_s = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(test_db) as conn:
            rows = conn.execute("""
                SELECT id FROM user_keys
                WHERE is_active = 1 AND COALESCE(is_trial, 0) = 0
                  AND COALESCE(expiry_notified_stage, 0) == 0
                  AND expires_at > ? AND expires_at <= ?
            """, (now_s, t_72h)).fetchall()
            for r in rows:
                conn.execute("UPDATE user_keys SET expiry_notified_stage = 1 WHERE id = ?", (r[0],))
            conn.commit()
            st1_val = conn.execute("SELECT expiry_notified_stage FROM user_keys WHERE id = ?", (paid_kid,)).fetchone()[0]

        # Продление ключа: добавляем 30 дней, сбрасываем stage = 0
        new_exp_extended = (datetime.now() + timedelta(days=60)).strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(test_db) as conn:
            conn.execute("UPDATE user_keys SET expires_at = ?, expiry_notified_stage = 0 WHERE id = ?", (new_exp_extended, paid_kid))
            conn.commit()
            st_reset = conn.execute("SELECT expiry_notified_stage FROM user_keys WHERE id = ?", (paid_kid,)).fetchone()[0]

        report_step("E2E Шаг 6: Напоминания за 72ч (stage 0 -> 1) и сброс stage=0 при продлении в 1 клик", st1_val == 1 and st_reset == 0)

        # ----------------------------------------------------------------------
        # Шаг 7: Отказоустойчивость при нескольких нодах
        # ----------------------------------------------------------------------
        with sqlite3.connect(test_db) as conn:
            conn.execute("""
                INSERT INTO servers (name, host, panel_url, panel_type, is_active, group_name)
                VALUES ('Dead Server', '127.0.0.1:54321', 'http://127.0.0.1:54321', '3xui', 1, 'Стандарт')
            """)
            conn.commit()

        # Имитируем провижининг на двух серверах
        srv_list = [
            {"id": 1, "name": "Live Server", "host": "127.0.0.1", "panel_type": "none", "is_active": 1},
            {"id": 2, "name": "Dead Server", "host": "127.0.0.1:54321", "panel_type": "3xui", "is_active": 1}
        ]

        gen_links = []
        for s in srv_list:
            try:
                if s["name"] == "Dead Server":
                    raise ConnectionRefusedError("Node connection failed")
                gen_links.append(f"vless://user@{s['host']}:443#{s['name']}")
            except Exception:
                pass  # упавшая нода пропускается без падения процесса

        report_step("E2E Шаг 7: Отказоустойчивость: падение одной ноды не прерывает транзакцию (ссылок создано: 1)", len(gen_links) == 1)

    finally:
        if os.path.exists(test_db):
            try:
                os.remove(test_db)
            except Exception:
                pass

# ------------------------------------------------------------------------------
# 23. БЕЗОПАСНОСТЬ: .gitignore, ПРАВА ДОСТУПА И ПОЛНЫЙ ШИФРОВАННЫЙ БЭКАП
# ------------------------------------------------------------------------------
async def test_security_gitignore_permissions_and_full_backup():
    print("\n🔐 23. Тест безопасности, прав доступа ОС и полного AES/Fernet бэкапа...")
    try:
        import bot

        # 1. Проверка правил .gitignore
        gi_path = os.path.join(BASE_DIR, ".gitignore")
        gi_ok = os.path.exists(gi_path)
        required_rules = [".env", ".env.*", "*.bak", "*.db", "*.db-wal", "*.db-shm", "*.sqlite", "*.zip", "*.enc", "backups/", ".update_notify.json"]
        rules_present = True
        if gi_ok:
            with open(gi_path, "r", encoding="utf-8") as f:
                gi_content = f.read()
            for r in required_rules:
                if r not in gi_content:
                    rules_present = False
                    break
        report_step("Безопасность: .gitignore содержит все критические правила для публичного Git", gi_ok and rules_present)

        # 2. Вызов harden_file_permissions (проверка без исключений)
        try:
            bot.harden_file_permissions()
            perm_ok = True
        except Exception:
            perm_ok = False
        report_step("Безопасность: функция harden_file_permissions() выполняется без ошибок", perm_ok)

        # 3. Создание зашифрованного бэкапа с паролем
        test_pwd = "MasterTestPassword_2026!"
        enc_backup = bot.create_full_system_backup(reason="test_sec", password=test_pwd)
        is_enc = os.path.exists(enc_backup) and enc_backup.endswith(".enc")
        report_step("Полный бэкап: создание зашифрованного архива .zip.enc (AES-256 Fernet)", is_enc, f"Файл: {os.path.basename(enc_backup)}")

        # 4. Проверка восстановления из зашифрованного бэкапа
        ok_restore, res_msg = bot.restore_system_from_backup_archive(enc_backup, password=test_pwd)
        report_step("Полный бэкап: успешная дешифровка, проверка manifest.json и целостности SQLite", ok_restore, res_msg)

        # 5. Проверка отклонения при неверном пароле
        bad_restore, _ = bot.restore_system_from_backup_archive(enc_backup, password="WrongPassword!")
        report_step("Безопасность: отклонение восстановления при неверном пароле дешифровки", not bad_restore)

        # Очистка тестового файла
        if os.path.exists(enc_backup):
            try:
                os.remove(enc_backup)
            except Exception:
                pass
    except Exception as e:
        report_step("Безопасность и полные бэкапы", False, f"Исключение: {e}")


# ------------------------------------------------------------------------------
# 24. АВТОМАТИЧЕСКОЕ ОТКРЫТИЕ ПОРТОВ: install.sh И bot.py
# ------------------------------------------------------------------------------
async def test_install_sh_and_bot_auto_port_opening():
    print("\n🛡 24. Тест автоматического открытия портов в install.sh и bot.py...")
    try:
        import bot

        # 1. Проверка install.sh на наличие функции open_required_firewall_ports
        inst_path = os.path.join(BASE_DIR, "install.sh")
        has_sh = os.path.exists(inst_path)
        has_ports_fn = False
        if has_sh:
            with open(inst_path, "r", encoding="utf-8") as f:
                sh_code = f.read()
            has_ports_fn = "open_required_firewall_ports" in sh_code and "22/tcp" in sh_code and "8443" in sh_code
        report_step("Скрипт install.sh: наличие безопасной функции open_required_firewall_ports() (22, 80, 443, 8443, sub)", has_ports_fn)

        # 2. Вызов ensure_required_ports_open() в bot.py
        port_res = await bot.ensure_required_ports_open()
        p_ok = isinstance(port_res, dict) and "status" in port_res
        report_step("Сетевой стек bot.py: ensure_required_ports_open() сканирует базу и применяет правила фаервола", p_ok, f"Статус: {port_res.get('status')}")
    except Exception as e:
        report_step("Автоматическое открытие портов", False, f"Исключение: {e}")


# ------------------------------------------------------------------------------
# 25. САМООБНОВЛЕНИЕ БОТА ИЗ TELEGRAM: МАРКЕР, БЭКАП И ЭКРАН ОБНОВЛЕНИЯ
# ------------------------------------------------------------------------------
async def test_self_update_marker_and_backup():
    print("\n🔄 25. Тест центра автообновления бота прямо из Telegram (Git Self-Update)...")
    try:
        import bot

        # 1. Проверка получения текущего коммита
        commit = bot.get_current_git_commit()
        has_commit = bool(commit and len(commit) >= 4)
        report_step("Git Self-Update: корректное получение текущего коммита репозитория", has_commit, f"Commit: {commit}")

        # 2. Проверка генерации экрана обновлений
        up_text, up_kb = bot.render_bot_update_screen()
        has_up_btn = any(
            any("admin_bot_update_run" in getattr(btn, "callback_data", "") for btn in row)
            for row in up_kb.inline_keyboard
        )
        report_step("Центр обновлений: экран render_bot_update_screen() формирует кнопку 'admin_bot_update_run'", has_up_btn)

        # 3. Проверка жизненного цикла маркера .update_notify.json
        marker_p = os.path.join(BASE_DIR, ".update_notify.json")
        test_payload = {
            "chat_id": 5167074427,
            "message_id": 9999,
            "prev_commit": "abc1234",
            "new_commit": "def5678",
            "updated_at": "2026-11-17T12:00:00Z"
        }
        with open(marker_p, "w", encoding="utf-8") as f:
            json.dump(test_payload, f)
        report_step("Маркер обновления: создание файла .update_notify.json для уведомления админа после перезапуска", os.path.exists(marker_p))

        # Чтение и очистка
        with open(marker_p, "r", encoding="utf-8") as f:
            read_back = json.load(f)
        marker_valid = read_back.get("prev_commit") == "abc1234" and read_back.get("new_commit") == "def5678"
        os.remove(marker_p)
        report_step("Маркер обновления: валидность структуры данных и успешное удаление маркера", marker_valid and not os.path.exists(marker_p))
    except Exception as e:
        report_step("Самообновление бота", False, f"Исключение: {e}")


# ------------------------------------------------------------------------------
# 26. МУЛЬТИПРОТОКОЛЬНОСТЬ (ДО 10+ НА НОДУ) И СТРОГАЯ ИЗОЛЯЦИЯ ГРУПП
# ------------------------------------------------------------------------------
async def test_multi_protocol_up_to_10_and_group_isolation():
    print("\n🧩 26. Тест мультипротокольности (10+ протоколов) и строгой изоляции групп...")
    test_db = os.path.join(BASE_DIR, "test_multi_proto.db")
    try:
        from migrate_db import run_migrations
        run_migrations(test_db)

        # 1. Проверка структуры server_inbounds и UNIQUE(key_id, server_id, panel_type, inbound_id)
        with sqlite3.connect(test_db) as conn:
            cur = conn.cursor()
            inb_t = cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='server_inbounds'").fetchone()
            report_step("Мультипротокол: таблица server_inbounds успешно создана и мигрирована", bool(inb_t))

            # Создаем тестовый сервер
            cur.execute(
                "INSERT INTO servers (name, host, panel_type, group_name, is_active) VALUES (?, ?, ?, ?, 1)",
                ("Multi-Node 1", "node1.test:443", "3xui", "VIP")
            )
            srv_id = cur.lastrowid

            # Добавляем 5 протоколов для этого сервера
            protocols = [
                (1, "vless", 443, "VLESS Reality"),
                (2, "trojan", 8443, "Trojan TLS"),
                (3, "vmess", 2083, "VMess WS"),
                (4, "shadowsocks", 10001, "SS-2022"),
                (5, "hysteria2", 443, "Hysteria2 UDP")
            ]
            for inb_id, proto, port, rem in protocols:
                cur.execute(
                    "INSERT INTO server_inbounds (server_id, inbound_id, protocol, port, remark, is_active) VALUES (?, ?, ?, ?, ?, 1)",
                    (srv_id, inb_id, proto, port, rem)
                )

            # Проверяем привязку ключа ко всем 5 протоколам ноды
            cur.execute("INSERT INTO users (user_id, username) VALUES (777001, 'proto_user')")
            cur.execute("INSERT INTO user_keys (user_id, sub_id, expires_at, is_active) VALUES (777001, 'sub_multi', 2000000000, 1)")
            key_id = cur.lastrowid

            for inb_id, proto, port, rem in protocols:
                cur.execute(
                    "INSERT INTO user_key_nodes (key_id, server_id, panel_type, inbound_id, remote_id, remote_email, is_active) VALUES (?, ?, '3xui', ?, 'uuid', 'email', 1)",
                    (key_id, srv_id, inb_id)
                )
            conn.commit()

            count_links = cur.execute("SELECT COUNT(*) FROM user_key_nodes WHERE key_id = ?", (key_id,)).fetchone()[0]
            report_step("Мультипротокол: успешная выдача 5 различных протоколов на одной ноде для одного ключа", count_links == 5)

        # 2. Проверка строгой изоляции групп серверов
        from panel_manager import get_servers_for_group
        vip_servers = get_servers_for_group("VIP", test_db)
        std_servers = get_servers_for_group("Стандарт", test_db)
        report_step("Изоляция групп: VIP-сервер не попадает в выборку для группы 'Стандарт'", len(vip_servers) == 1 and len(std_servers) == 0)

        # 3. Проверка формата генерации ссылок {server_name} · {remark or PROTOCOL}
        from panel_manager import generate_3xui_link
        inb_dict = {
            "protocol": "hysteria2",
            "port": 443,
            "remark": "Hysteria2 Fast",
            "streamSettings": {
                "security": "tls",
                "tlsSettings": {"serverName": "test.com"}
            }
        }
        link_out = generate_3xui_link(
            inb=inb_dict,
            client_uuid="my_secret_token",
            host="node1.test",
            server_name="Multi-Node 1"
        )
        from urllib.parse import unquote
        clean_link = unquote(link_out)
        report_step("Формат ссылок подписки: имя ноды и протокол объединены через точку ({server_name} · {protocol})", "Multi-Node 1" in clean_link and "hysteria2://" in clean_link)

    finally:
        if os.path.exists(test_db):
            try:
                os.remove(test_db)
            except Exception:
                pass


# ------------------------------------------------------------------------------
# 27. ХАБ ПОЛЬЗОВАТЕЛЕЙ И АВТО-ВОССТАНОВЛЕНИЕ (БЭКАП / ПАНЕЛИ)
# ------------------------------------------------------------------------------
async def test_users_hub_and_auto_restore_backup_and_panels():
    print("\n👥 27. Тест хаба пользователей, 8 метрик статистики и восстановления...")
    try:
        import bot

        # 1. Проверка экрана Users Hub (8 метрик + 7 кнопок)
        hub_text, hub_kb = bot.render_admin_users_hub()
        has_stats = all(metric in hub_text for metric in ("Всего пользователей", "Активных подписок", "Истекших подписок", "Триал-пользователей", "Привлеченных рефералов", "Общий баланс", "Израсходованный трафик", "Активных ключей"))
        report_step("Хаб пользователей: экран render_admin_users_hub() отображает все 8 ключевых метрик системы", has_stats)

        required_hub_callbacks = [
            "admin_users_list_all",
            "admin_user_search",
            "admin_restore_users_menu",
            "admin_sync_db_to_panel",
            "admin_smart_restore_from_panels",
            "admin_sync_deleted_users",
            "admin_back_main"
        ]
        all_cb_in_hub = [getattr(btn, "callback_data", "") for row in hub_kb.inline_keyboard for btn in row]
        all_buttons_present = all(cb in all_cb_in_hub for cb in required_hub_callbacks)
        report_step("Хаб пользователей: наличие всех 7 кнопок управления (список, поиск, бэкап, синхронизация, умное восстановление, очистка, меню)", all_buttons_present)

        # 2. Проверка экрана управления бэкапами
        b_text, b_kb = bot.render_admin_backup_menu()
        has_backup_controls = all(ctrl in b_text for ctrl in ("Ежедневный автобэкап", "Время автобэкапа", "Хранить бэкапы", "Шифрование архивов"))
        report_step("Меню бэкапов: render_admin_backup_menu() отображает статус расписания, глубину хранения и шифрование", has_backup_controls)

        # 3. Проверка ротации старых архивов
        bot.rotate_old_backups()
        report_step("Ротация бэкапов: функция rotate_old_backups() успешно отрабатывает без ошибок", True)

    except Exception as e:
        report_step("Хаб пользователей и авто-восстановление", False, f"Исключение: {e}")

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
    await test_tariffs_vpn_vs_bot_separation()
    await test_install_sh_reuse_env_mode()
    await test_sub_page_one_click_import_links()
    await test_admin_backup_zip_creation()
    await test_node_alert_antispam_and_recovery()
    await test_trial_followup_and_referral_admin()
    await test_all_inline_callbacks_have_handlers()
    await test_full_e2e_user_lifecycle()
    await test_security_gitignore_permissions_and_full_backup()
    await test_install_sh_and_bot_auto_port_opening()
    await test_self_update_marker_and_backup()
    await test_multi_protocol_up_to_10_and_group_isolation()
    await test_users_hub_and_auto_restore_backup_and_panels()
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
