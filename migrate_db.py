#!/usr/bin/env python3
"""
Скрипт и модуль автоматической идемпотентной миграции БД Bot_VIP (bot_data.db).
Поддерживает безопасное обновление схемы SQLite без потери существующих данных.
"""

import os
import sys
import glob
import sqlite3
import logging

logger = logging.getLogger("Bot_VIP.MigrateDB")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("DB_PATH", os.path.join(BASE_DIR, "bot_data.db"))


def run_migrations(db_path: str = None) -> bool:
    """
    Выполняет полную идемпотентную миграцию указанной SQLite БД:
    1. Перевод в режим WAL и увеличение busy_timeout до 30 секунд.
    2. Создание всех системных таблиц проекта (CREATE TABLE IF NOT EXISTS).
    3. Добавление отсутствующих колонок через ALTER TABLE ADD COLUMN.
    4. Создание необходимых индексов для ускорения поиска.
    """
    target = db_path or DB_PATH
    db_dir = os.path.dirname(os.path.abspath(target))
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)

    try:
        conn = sqlite3.connect(target, timeout=30.0)
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 30000;")
        cur = conn.cursor()

        # ----------------------------------------------------------------------
        # 1. ТАБЛИЦЫ
        # ----------------------------------------------------------------------
        # users
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                joined_at TEXT,
                is_vip INTEGER DEFAULT 0,
                is_banned INTEGER DEFAULT 0,
                vip_until TEXT DEFAULT '',
                failed_attempts INTEGER DEFAULT 0,
                vpn_config TEXT DEFAULT '',
                bot_license TEXT DEFAULT '',
                server_id INTEGER DEFAULT 0,
                sub_id TEXT DEFAULT '',
                client_uuid TEXT DEFAULT '',
                trial_used INTEGER DEFAULT 0,
                has_expired INTEGER DEFAULT 0,
                bot_blocked INTEGER DEFAULT 0,
                referred_by INTEGER DEFAULT 0,
                referrals_count INTEGER DEFAULT 0,
                ref_bonus_days INTEGER DEFAULT 0,
                active_discount_pct INTEGER DEFAULT 0,
                balance REAL DEFAULT 0,
                referrer_id INTEGER DEFAULT 0
            );
        """)

        # user_keys
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                key_name TEXT DEFAULT '',
                client_uuid TEXT DEFAULT '',
                sub_id TEXT DEFAULT '',
                server_id INTEGER DEFAULT 0,
                group_name TEXT DEFAULT 'Стандарт',
                location_name TEXT DEFAULT '🇩🇪 Германия',
                traffic_bytes INTEGER DEFAULT 0,
                limit_gb REAL DEFAULT 0,
                limit_ip INTEGER DEFAULT 1,
                expires_at TEXT DEFAULT '',
                config_text TEXT DEFAULT '',
                is_active INTEGER DEFAULT 1,
                warned_24h INTEGER DEFAULT 0,
                warned_3h INTEGER DEFAULT 0,
                history_log TEXT DEFAULT '',
                created_at TEXT
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_user_keys_uid ON user_keys(user_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_user_keys_sub ON user_keys(sub_id);")

        # servers
        cur.execute("""
            CREATE TABLE IF NOT EXISTS servers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                host TEXT,
                panel_type TEXT DEFAULT '3xui',
                panel_url TEXT DEFAULT '',
                panel_user TEXT DEFAULT '',
                panel_pass TEXT DEFAULT '',
                inbound_id INTEGER DEFAULT 1,
                sub_url_prefix TEXT DEFAULT '',
                proxy_template TEXT DEFAULT '',
                is_active INTEGER DEFAULT 1,
                group_name TEXT DEFAULT 'Стандарт',
                wpp_token TEXT DEFAULT '',
                wpp_url TEXT DEFAULT '',
                created_at TEXT
            );
        """)

        # user_key_nodes
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_key_nodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key_id INTEGER NOT NULL,
                server_id INTEGER NOT NULL,
                panel_type TEXT DEFAULT '3xui',
                remote_id TEXT DEFAULT '',
                remote_email TEXT DEFAULT '',
                up_bytes INTEGER DEFAULT 0,
                down_bytes INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                updated_at TEXT,
                UNIQUE(key_id, server_id, panel_type)
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ukn_key ON user_key_nodes(key_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ukn_srv ON user_key_nodes(server_id);")

        # tariffs
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tariffs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_name TEXT DEFAULT 'Стандарт',
                name TEXT,
                days INTEGER,
                price_rub INTEGER,
                price_stars INTEGER DEFAULT 100,
                max_devices INTEGER DEFAULT 1,
                sort_order INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                is_bot INTEGER DEFAULT 0
            );
        """)

        # bot_licenses
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_licenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lic_key TEXT UNIQUE,
                buyer_note TEXT DEFAULT '',
                bound_admin_id TEXT DEFAULT '',
                bound_ip TEXT DEFAULT '',
                server_ip TEXT DEFAULT '',
                server_hwid TEXT DEFAULT '',
                expires_id TEXT DEFAULT '',
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # payment_cards
        cur.execute("""
            CREATE TABLE IF NOT EXISTS payment_cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                card_number TEXT,
                bank_name TEXT DEFAULT '',
                holder_name TEXT DEFAULT '',
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # key_devices
        cur.execute("""
            CREATE TABLE IF NOT EXISTS key_devices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key_id INTEGER NOT NULL,
                hwid TEXT NOT NULL,
                sub_token TEXT DEFAULT '',
                device_name TEXT DEFAULT '',
                ip TEXT DEFAULT '',
                last_ip TEXT DEFAULT '',
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(key_id, hwid)
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_key_devices_key ON key_devices(key_id);")

        # settings
        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        """)

        # promocodes & promo_usages
        cur.execute("""
            CREATE TABLE IF NOT EXISTS promocodes (
                code TEXT PRIMARY KEY,
                days INTEGER DEFAULT 7,
                max_uses INTEGER DEFAULT 100,
                used_count INTEGER DEFAULT 0,
                created_at TEXT
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS promo_usages (
                code TEXT,
                user_id INTEGER,
                used_at TEXT,
                PRIMARY KEY (code, user_id)
            );
        """)

        # coupons & coupon_usages
        cur.execute("""
            CREATE TABLE IF NOT EXISTS coupons (
                code TEXT PRIMARY KEY,
                discount_pct INTEGER DEFAULT 20,
                max_uses INTEGER DEFAULT 100,
                used_count INTEGER DEFAULT 0,
                created_at TEXT
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS coupon_usages (
                code TEXT,
                user_id INTEGER,
                used_at TEXT,
                PRIMARY KEY (code, user_id)
            );
        """)

        # withdrawals
        cur.execute("""
            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL DEFAULT 0,
                requisites TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # license_keys
        cur.execute("""
            CREATE TABLE IF NOT EXISTS license_keys (
                key_code TEXT PRIMARY KEY,
                key_type TEXT DEFAULT 'VPN',
                days INTEGER,
                price REAL DEFAULT 0,
                is_used INTEGER DEFAULT 0,
                used_by INTEGER DEFAULT 0,
                used_at TEXT DEFAULT ''
            );
        """)

        # payments
        cur.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL DEFAULT 0,
                provider TEXT DEFAULT '',
                description TEXT,
                created_at TEXT
            );
        """)

        # vpn_pool
        cur.execute("""
            CREATE TABLE IF NOT EXISTS vpn_pool (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                server_id INTEGER DEFAULT 0,
                config_data TEXT,
                is_assigned INTEGER DEFAULT 0,
                assigned_to INTEGER DEFAULT 0,
                assigned_at TEXT DEFAULT ''
            );
        """)

        # user_logs
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                first_name TEXT,
                event_type TEXT,
                content TEXT,
                created_at TEXT
            );
        """)

        # pending_invoices
        cur.execute("""
            CREATE TABLE IF NOT EXISTS pending_invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                inv_id TEXT UNIQUE,
                provider TEXT,
                user_id INTEGER,
                t_id INTEGER,
                days INTEGER,
                price REAL,
                tariff_name TEXT,
                is_bot_lic INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # bot_staff
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_staff (
                user_id INTEGER PRIMARY KEY,
                role TEXT DEFAULT 'moderator',
                added_by INTEGER DEFAULT 0,
                created_at TEXT
            );
        """)

        # ----------------------------------------------------------------------
        # 2. МИГРАЦИЯ КОЛОНОК (ALTER TABLE ADD COLUMN)
        # ----------------------------------------------------------------------
        def add_cols_if_missing(table_name: str, cols_dict: dict):
            existing = [c[1] for c in cur.execute(f"PRAGMA table_info({table_name})").fetchall()]
            for col, col_def in cols_dict.items():
                if col not in existing:
                    cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {col} {col_def};")

        # users
        add_cols_if_missing("users", {
            "trial_used": "INTEGER DEFAULT 0",
            "trial_expires_at": "TEXT DEFAULT ''",
            "trial_followup_stage": "INTEGER DEFAULT 0",
            "balance": "REAL DEFAULT 0",
            "referrer_id": "INTEGER DEFAULT 0",
            "is_vip": "INTEGER DEFAULT 0",
            "is_banned": "INTEGER DEFAULT 0",
            "vip_until": "TEXT DEFAULT ''",
            "failed_attempts": "INTEGER DEFAULT 0",
            "vpn_config": "TEXT DEFAULT ''",
            "bot_license": "TEXT DEFAULT ''",
            "server_id": "INTEGER DEFAULT 0",
            "sub_id": "TEXT DEFAULT ''",
            "client_uuid": "TEXT DEFAULT ''",
            "has_expired": "INTEGER DEFAULT 0",
            "bot_blocked": "INTEGER DEFAULT 0",
            "referred_by": "INTEGER DEFAULT 0",
            "referrals_count": "INTEGER DEFAULT 0",
            "ref_bonus_days": "INTEGER DEFAULT 0",
            "active_discount_pct": "INTEGER DEFAULT 0"
        })

        # user_keys
        add_cols_if_missing("user_keys", {
            "group_name": "TEXT DEFAULT 'Стандарт'",
            "sub_id": "TEXT DEFAULT ''",
            "traffic_bytes": "INTEGER DEFAULT 0",
            "limit_gb": "REAL DEFAULT 0",
            "limit_ip": "INTEGER DEFAULT 1",
            "client_uuid": "TEXT DEFAULT ''",
            "config_text": "TEXT DEFAULT ''",
            "is_active": "INTEGER DEFAULT 1",
            "warned_24h": "INTEGER DEFAULT 0",
            "warned_3h": "INTEGER DEFAULT 0",
            "history_log": "TEXT DEFAULT ''"
        })

        # servers
        add_cols_if_missing("servers", {
            "group_name": "TEXT DEFAULT 'Стандарт'",
            "panel_type": "TEXT DEFAULT '3xui'",
            "wpp_token": "TEXT DEFAULT ''",
            "wpp_url": "TEXT DEFAULT ''",
            "panel_user": "TEXT DEFAULT ''",
            "panel_pass": "TEXT DEFAULT ''",
            "is_active": "INTEGER DEFAULT 1",
            "inbound_id": "INTEGER DEFAULT 1",
            "sub_url_prefix": "TEXT DEFAULT ''",
            "proxy_template": "TEXT DEFAULT ''"
        })

        # user_key_nodes
        add_cols_if_missing("user_key_nodes", {
            "panel_type": "TEXT DEFAULT '3xui'",
            "remote_id": "TEXT DEFAULT ''",
            "remote_email": "TEXT DEFAULT ''",
            "up_bytes": "INTEGER DEFAULT 0",
            "down_bytes": "INTEGER DEFAULT 0",
            "is_active": "INTEGER DEFAULT 1",
            "updated_at": "TEXT"
        })

        # tariffs
        add_cols_if_missing("tariffs", {
            "group_name": "TEXT DEFAULT 'Стандарт'",
            "max_devices": "INTEGER DEFAULT 1",
            "sort_order": "INTEGER DEFAULT 0",
            "is_active": "INTEGER DEFAULT 1",
            "is_bot": "INTEGER DEFAULT 0"
        })

        # withdrawals
        add_cols_if_missing("withdrawals", {
            "user_id": "INTEGER DEFAULT 0",
            "amount": "REAL DEFAULT 0",
            "requisites": "TEXT DEFAULT ''",
            "status": "TEXT DEFAULT 'pending'",
            "created_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
        })

        # Авторазметка тарифов продажи/аренды бота
        try:
            tar_cols = [c[1] for c in cur.execute("PRAGMA table_info(tariffs)").fetchall()]
            cur.execute("""
                UPDATE tariffs
                SET is_bot = 1
                WHERE (
                    name LIKE '%Старт 30 дней%'
                    OR name LIKE '%Квартал (90 дней)%'
                    OR name LIKE '%Полгода (180 дней)%'
                    OR name LIKE '%Годовая подписка (365 дней)%'
                    OR name LIKE '%Безлимит (LifeTime)%'
                    OR group_name = 'Лицензия Бота'
                    OR group_name LIKE '%Бот%'
                ) AND (is_bot IS NULL OR is_bot = 0)
            """)
            if "category" in tar_cols:
                cur.execute("UPDATE tariffs SET is_bot = 1 WHERE category = 'bot'")
            if "tariff_type" in tar_cols:
                cur.execute("UPDATE tariffs SET is_bot = 1 WHERE tariff_type = 'bot'")
        except Exception as e:
            logger.warning(f"Ошибка авторазметки тарифов бота: {e}")

        conn.commit()
        conn.close()
        logger.info(f"✅ Миграция БД успешно завершена для {target}")
        return True

    except Exception as e:
        logger.error(f"❌ Ошибка миграции БД ({target}): {e}")
        return False


def run_all_detected_databases():
    """Сканирует и мигрирует все обнаруженные SQLite базы данных."""
    patterns = [
        DB_PATH,
        os.path.join(BASE_DIR, "*.db"),
        os.path.join(BASE_DIR, "data", "*.db"),
        "/opt/Bot_VIP/*.db",
        "/opt/Bot_VIP/data/*.db",
    ]
    db_files = set()
    for pattern in patterns:
        for f in glob.glob(pattern):
            db_files.add(os.path.abspath(f))

    if not db_files:
        db_files.add(os.path.abspath(DB_PATH))

    for p in sorted(db_files):
        print(f"⏳ Применение миграций: {p}")
        ok = run_migrations(p)
        status = "✅ УСПЕШНО" if ok else "❌ ОШИБКА"
        print(f"  {status}: {p}")


if __name__ == "__main__":
    run_all_detected_databases()
