# 🗺️ Маршрутный лист и архитектурная карта проекта `Bot_VIP`

> [!NOTE]
> Данный документ является генеральным планом и технической картой мульти-панельного VPN-бота `Bot_VIP`. В нём зафиксированы цели проекта, структура файлов, зоны ответственности каждого модуля, схема базы данных и сквозные потоки данных.

---

## 1. Чего мы добиваемся (Глобальная цель и видение)

Главная цель проекта — создание **отказоустойчивого, полностью автономного коммерческого VPN-бота** с гибридной архитектурой нового поколения:

1. **Мульти-панельная гибридная выдача (`3X-UI` + `WPP`):**
   - Пользователь покупает один тариф и получает **одну ссылку подписки** (`/sub/{sub_id}`), которая объединяет серверы классического Xray (через 3X-UI: VLESS Reality, VMess, Trojan) и прокси-панели Web-Panel-Proxy (через WPP: VLESS xhttp, Hysteria2).
   - Если провайдер пользователя блокирует один протокол (например, TCP/TLS), клиентское приложение автоматически переключается на альтернативный (например, UDP Hysteria2 или HTTP/3).

2. **Строгая привязка серверов к тарифам (Матрица «Тариф $\rightarrow$ Серверы»):**
   - Разделение серверов по группам («Стандарт», «VIP», «VPN + Proxy», «Акции»).
   - Клиент тарифа «Стандарт» получает доступ только к стандартным нодам, а VIP-клиент — ко всем приоритетным серверам. При этом предусмотрен безопасный фоллбэк: если под группу нет выделенной ноды, бот не падает, а выдает базовые рабочие сервера.

3. **Сквозная отказоустойчивость (Fault Tolerance):**
   - Ни при каких обстоятельствах падение, перезагрузка или сетевой таймаут отдельной ноды (3X-UI или WPP) не должны приводить к ошибкам HTTP 500 для пользователя или сбою фоновых задач планировщика.
   - Недоступные ноды мягко пропускаются, отдаются рабочие конфигурации, а трафик подтягивается из локального кэша SQLite.

4. **Автономный жизненный цикл клиента:**
   - Автоматический прием оплат (Telegram Stars, YooKassa, CryptoPay, СБП/карты).
   - Мгновенная регистрация клиента на всех серверах тарифа через REST API.
   - Фоновый мониторинг трафика каждые 5 минут и суммирование объемов с обеих панелей.
   - Предупреждения об окончании срока (за 24ч и за 3ч).
   - Автоматическое отключение на удаленных нодах при исчерпании лимита гигабайт или истечении срока.
   - Защита от передачи ссылки третьим лицам через ограничение HWID устройств.

---

## 2. Что где лежит (Файловая структура проекта)

Рабочая директория проекта: `C:\Users\PC\Desktop\BotAnti\Bot_VIP`

```
Bot_VIP/
│
├── ⚙️ ЯДРО И БОТ
│   ├── bot.py                     # Основное приложение: Telegram-бот, планировщик задач, Sub-сервер
│   ├── .env                       # Конфигурация окружения (токены, порты, ID админа, ключи WPP)
│   ├── .env.example               # Шаблон файла окружения
│   ├── requirements.txt           # Список Python-зависимостей проекта
│   └── start_bot.bat              # Скрипт быстрого запуска бота для Windows
│
├── 🌐 МУЛЬТИ-ПАНЕЛЬ И СЕТЬ
│   ├── panel_manager.py           # MultiPanelManager: фасад для 3X-UI и WPP, генератор ссылок Reality
│   ├── wpp_client.py              # Асинхронный HTTPX-клиент для API Web-Panel-Proxy (WPP)
│   ├── sub_server.py              # Единый генератор подписки (/sub/{sub_id}) для приложений и браузеров
│   ├── sub_dashboard.py           # Генератор адаптивного HTML-дашборда веб-подписки
│   └── sub_service.py             # Сервисный слой формирования подписок и QR-кодов
│
├── 🗄️ БАЗА ДАННЫХ И ХРАНИЛИЩЕ
│   ├── bot_data.db                # Основная SQLite база данных (WAL-режим, таймаут 30с)
│   ├── migrate_db.py              # Скрипты ручной миграции БД
│   └── backup_manager.py          # Автоматическое и ручное резервное копирование SQLite БД
│
├── 💳 ПЛАТЕЖИ И ФИНАНСЫ
│   ├── autopay.py                 # Проверка счетов YooKassa и CryptoPay в фоновом режиме
│   └── payment_cards.py           # Управление ручными реквизитами для оплат картой / СБП
│
├── 🎛️ АДМИНИСТРИРОВАНИЕ И ВИЗАРДЫ
│   ├── server_wizard.py           # Интерактивный визард добавления/настройки серверов в Telegram
│   ├── tariff_wizard.py           # Интерактивный визард создания/редактирования тарифов
│   └── staff_roles.py             # Разграничение прав персонала (Администраторы, Модераторы, Dev)
│
├── 🧪 ТЕСТИРОВАНИЕ И ОТЛАДКА
│   ├── test_system.py             # Автономный комплексный смоук-тест (15 тестов)
│   ├── bot.log                    # Лог-файл работы основного процесса бота
│   └── sub_server.log             # Лог-файл внешнего HTTP-сервера подписок
│
└── 🐧 РАЗВЁРТЫВАНИЕ НА LINUX
    ├── install.sh                 # Скрипт первичной авто-установки бота на Linux VPS
    ├── backup_job.sh              # Bash-скрипт бэкапа по cron
    └── update_v2.tar.gz           # Архив релизного обновления
```

---

## 3. Что за что отвечает (Компоненты и зоны ответственности)

| Файл / Модуль | Основная зона ответственности |
|---|---|
| [bot.py](file:///C:/Users/PC/Desktop/BotAnti/Bot_VIP/bot.py) | **Сердце проекта.** Запуск Telegram Application, меню клиентов и админки, встроенный сервер подписок на порту 2096, регистрация 5 фоновых задач, авто-миграция БД при старте, обработка оплат (Stars, YooKassa, CryptoPay). |
| [panel_manager.py](file:///C:/Users/PC/Desktop/BotAnti/Bot_VIP/panel_manager.py) | **Мульти-панельный фасад (`MultiPanelManager`).** Добавление клиентов, сбор трафика и отключение на 3X-UI и WPP. Парсинг инбаундов Xray, корректное извлечение Reality `pbk` и фильтрация служебных нод. |
| [wpp_client.py](file:///C:/Users/PC/Desktop/BotAnti/Bot_VIP/wpp_client.py) | **Клиент WPP.** Асинхронные HTTPX-запросы к `/wpp-api/v1/profiles/*`. Управление токенами авторизации, формирование Bearer-заголовков, защита от ошибок 401 и маскированное логирование. |
| [sub_server.py](file:///C:/Users/PC/Desktop/BotAnti/Bot_VIP/sub_server.py) | **Единый генератор подписки.** Функция `build_subscription_payload()`. Отдает Base64 с заголовками `Subscription-Userinfo` для VPN-клиентов и HTML-дашборд для браузеров. Проверяет HWID-лимит устройств. |
| [sub_dashboard.py](file:///C:/Users/PC/Desktop/BotAnti/Bot_VIP/sub_dashboard.py) | **Интерфейс веб-кабинета.** Красивый Web-UI, отображающий остаток трафика, статус ключа, дату окончания, QR-код подписки и кнопки копирования ссылок. |
| [test_system.py](file:///C:/Users/PC/Desktop/BotAnti/Bot_VIP/test_system.py) | **Система контроля качества.** Автоматический аудит БД, матрицы тарифов, ссылки Reality, WPP-авторизации, отдачи подписки и 5 фоновых задач. |
| [server_wizard.py](file:///C:/Users/PC/Desktop/BotAnti/Bot_VIP/server_wizard.py) | **Мастер серверов.** Пошаговое добавление нод в Telegram: хост, тип панели (3x-ui, wpp, both), URL панели, логин, пароль, группа. |
| [autopay.py](file:///C:/Users/PC/Desktop/BotAnti/Bot_VIP/autopay.py) | **Атомарный биллинг.** Проверка статусов инвойсов YooKassa/CryptoPay с защитой от повторного начисления подписки (атомарное списание инвойса). |

---

## 4. Схема базы данных SQLite (`bot_data.db`)

База данных работает в высокопроизводительном режиме **WAL (Write-Ahead Logging)** с таймаутом ожидания блокировки `busy_timeout = 30000` мс.

```mermaid
erDiagram
    users ||--o{ user_keys : "владеет"
    users ||--o{ payments : "совершает"
    tariffs ||--o{ user_keys : "определяет параметры"
    servers ||--o{ user_key_nodes : "содержит профиль"
    user_keys ||--o{ user_key_nodes : "привязан к нодам"
    user_keys ||--o{ key_devices : "ограничен по HWID"

    users {
        int user_id PK
        string username
        int is_vip
        string vip_until
        string sub_id
        string client_uuid
        int trial_used
    }

    tariffs {
        int id PK
        string group_name "Стандарт / VIP / Акции"
        string name
        int days
        int price_rub
        int price_stars
        int is_active
    }

    servers {
        int id PK
        string name "🇵🇱 Польша"
        string host "pl.domain:10027"
        string panel_type "3xui / wpp / both"
        string panel_url
        string panel_user "Логин 3X-UI"
        string panel_pass "Пароль 3X-UI"
        string group_name "Группа тарифа"
        string wpp_token "Отдельный API-токен WPP"
        string wpp_url "URL WPP API (порт 8090)"
        int is_active
    }

    user_keys {
        int id PK
        int user_id FK
        string client_uuid
        string sub_id UK
        string group_name
        string expires_at
        real limit_gb
        int traffic_bytes
        string config_text
        int is_active
    }

    user_key_nodes {
        int id PK
        int key_id FK
        int server_id FK
        string panel_type "3xui / wpp"
        string remote_id "client_uuid или WPP profile_id"
        string remote_email "user_X_kY"
        int up_bytes
        int down_bytes
        int is_active
        string updated_at
    }

    key_devices {
        int id PK
        int key_id FK
        string sub_token
        string hwid
        string last_ip
    }
```

---

## 5. Архитектура и сквозные потоки данных

### 5.1. Общая схема взаимодействия компонентов

```mermaid
flowchart TD
    subgraph Clients["Клиенты"]
        TGUser["Пользователь Telegram"]
        VPNApp["VPN Клиент (Happ, Streisand, v2rayTun)"]
        Browser["Веб-браузер"]
    end

    subgraph Entry["Точки входа"]
        TGBot["Telegram Bot (bot.py)"]
        SubServer["HTTP Sub Server (Порт 2097 / 2096)"]
    end

    subgraph Core["Ядро системы"]
        MPM["MultiPanelManager (panel_manager.py)"]
        SubBuilder["build_subscription_payload()"]
        DB[(SQLite: bot_data.db\nWAL Mode)]
    end

    subgraph Panels["Панели управления на нодах"]
        XUI["3X-UI Panel\n(VLESS Reality, VMess, Trojan)"]
        WPP["Web-Panel-Proxy (WPP)\n(VLESS xhttp, Hysteria2)"]
    end

    TGUser -->|Команды, оплата, меню| TGBot
    VPNApp -->|GET /sub/sub_id| SubServer
    Browser -->|GET /sub/sub_id| SubServer

    TGBot --> MPM
    TGBot --> DB
    SubServer --> SubBuilder
    SubBuilder --> DB

    MPM -->|REST API + CSRF| XUI
    MPM -->|REST API Bearer| WPP
```

---

### 5.2. Жизненный цикл покупки и выдачи подписки

```mermaid
sequenceDiagram
    autonumber
    actor User as Пользователь
    participant Bot as Telegram Bot
    participant MPM as MultiPanelManager
    participant XUI as 3X-UI Нода
    participant WPP as WPP Нода
    participant DB as База Данных SQLite

    User->>Bot: Оплата тарифа («Стандарт» / 30 дней)
    Bot->>DB: Поиск серверов по группе get_servers_for_group('Стандарт')
    DB-->>Bot: Список активных серверов
    Bot->>DB: Создание user_keys (генерация sub_id, client_uuid)
    
    par Регистрация на 3X-UI
        Bot->>MPM: provision_on_server(3xui)
        MPM->>XUI: POST /panel/api/clients/add (email: user_123_k1)
        XUI-->>MPM: Inbounds list
        MPM-->>Bot: VLESS Reality ссылка (с валидным pbk=)
    and Регистрация на WPP
        Bot->>MPM: provision_on_server(wpp)
        MPM->>WPP: POST /wpp-api/v1/profiles/create
        WPP-->>MPM: Profile bundle (id, link)
        MPM-->>Bot: Hysteria2 / VLESS xhttp ссылки
    end

    Bot->>DB: Сохранение профилей в user_key_nodes (remote_id, remote_email)
    Bot->>DB: Сохранение склеенных конфигов в user_keys.config_text
    Bot-->>User: Сообщение с кнопками, QR-кодом и единой ссылкой /sub/{sub_id}
```

---

### 5.3. Цикл фонового обслуживания (5 задач планировщика)

```mermaid
flowchart LR
    subgraph Scheduler["Планировщик APScheduler / JobQueue"]
        J1["1. poll_pending_invoices_job\n(каждые 25 сек)"]
        J2["2. check_expiry_reminders_job\n(каждые 30 мин)"]
        J3["3. sync_traffic_job\n(каждые 5 мин)"]
        J4["4. expire_vips_job\n(каждый 1 час)"]
        J5["5. daily_backup_job\n(раз в сутки в 03:00)"]
    end

    J1 -->|Проверка YooKassa / CryptoPay| ActKey["Активация ключа при поступлении денег"]
    J2 -->|Срок <= 24ч или <= 3ч| AlertUser["Уведомление клиента о продлении"]
    J3 -->|Опрос нод 3X-UI + WPP| TrafficCheck{"Превышен limit_gb?"}
    TrafficCheck -- Да --> Cutoff["Деактивация на серверах + отключение"]
    TrafficCheck -- Нет --> SaveStats["Сохранение up/down в user_key_nodes"]
    J4 -->|Срок истёк| DeactAll["Отключение на всех серверах 3X-UI и WPP"]
    J5 -->|Создание snapshot БД| SendBackup["Отправка .db файла админу в TG"]
```

---

## 6. Текущий статус готовности и чек-лист

### ✅ Что уже полностью сделано и протестировано:
- [x] **Этап 1:** Исправлен баг генерации Reality `pbk=` в 3X-UI (глубокий разбор `streamSettings`).
- [x] **Этап 2:** Проведён ресёрч WPP API, устранены синхронные блокировки потока, написан асинхронный клиент.
- [x] **Этап 3:** Создан единый агрегатор `build_subscription_payload`, объединяющий конфиги и отдающий Base64 клиентам и Web-UI браузерам.
- [x] **Этап 4:** Внедрена таблица `user_key_nodes`, матрица «Тариф $\rightarrow$ Серверы», 5-я фоновая задача `sync_traffic_job` и деактивация в `expire_vips_job`.
- [x] **Отладка WPP:** Исправлена ошибка `401 Unauthorized`: токен WPP изолирован от пароля 3X-UI при `panel_type = 'both'`.
- [x] **Тестирование:** Скрипт `test_system.py` подтвердил 15/15 успешных тестов.

### 📋 Что осталось сделать при переносе на боевой сервер (Production):
1. **Синхронизация файлов на VPS:**
   - Скопировать обновленные `bot.py`, `panel_manager.py`, `wpp_client.py`, `sub_server.py`, `test_system.py` на сервер в `/opt/Bot_VIP/`.
2. **Проверка реквизитов WPP в `.env`:**
   - Убедиться, что в `/opt/Bot_VIP/.env` прописан реальный токен WPP панели (`WPP_API_KEY=...`).
3. **Проверка Caddy:**
   - Убедиться, что Caddy проксирует `/sub/*` на порт `2097`, а `/wpp-api/*` — на порт `8090`.
4. **Тестовый прогон `python3 test_system.py` на VPS:**
   - Запустить скрипт прямо на Linux сервере для финального подтверждения связи с локальными демонами 3X-UI и WPP.
