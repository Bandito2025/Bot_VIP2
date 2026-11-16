#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────
#  Bot_VIP2 — Интерактивный и автоматический установщик
#  Репозиторий: https://github.com/Bandito2025/Bot_VIP2.git (main)
# ─────────────────────────────────────────────────────────────

RECONFIGURE=false
for arg in "$@"; do
    case "$arg" in
        --reconfigure|-r)
            RECONFIGURE=true
            shift
            ;;
    esac
done

# 1. Проверка запуска от root
if [[ $EUID -ne 0 ]]; then
    echo "❌ Ошибка: скрипт должен быть запущен с правами root!"
    echo "   Выполните: sudo bash install.sh"
    exit 1
fi

echo "=========================================================="
echo "   🚀 Установка / Обновление Bot_VIP2 — VPN Бот"
echo "=========================================================="
echo ""

BOT_DIR="/opt/Bot_VIP"
ENV_FILE="$BOT_DIR/.env"
BACKUP_ENV="/root/env.bak"

# Заранее определяем внешний IPv4
echo "⏳ Определение внешнего IP-адреса сервера..."
SERVER_IP=$(curl -s4 --max-time 10 ifconfig.me || curl -s4 --max-time 10 api.ipify.org || echo "")
if [[ -z "$SERVER_IP" ]]; then
    SERVER_IP=$(hostname -I | awk '{print $1}')
fi
echo "   Внешний IP сервера: $SERVER_IP"
echo ""

USE_EXISTING_ENV=false

# Проверка существующей конфигурации
if [[ "$RECONFIGURE" != true ]]; then
    if [[ ! -f "$ENV_FILE" && -f "$BACKUP_ENV" ]]; then
        mkdir -p "$BOT_DIR"
        cp -f "$BACKUP_ENV" "$ENV_FILE"
    fi

    if [[ -f "$ENV_FILE" ]]; then
        EXISTING_TOKEN=$(grep -E '^[[:space:]]*BOT_TOKEN=' "$ENV_FILE" | cut -d= -f2- | tr -d '[:space:]' | tr -d '"' | tr -d "'" || echo "")
        EXISTING_ADMIN=$(grep -E '^[[:space:]]*ADMIN_ID=' "$ENV_FILE" | cut -d= -f2- | tr -d '[:space:]' | tr -d '"' | tr -d "'" || echo "")
        EXISTING_SUB_URL=$(grep -E '^[[:space:]]*SUB_BASE_URL=' "$ENV_FILE" | cut -d= -f2- | tr -d '[:space:]' | tr -d '"' | tr -d "'" || echo "")

        if [[ -n "$EXISTING_TOKEN" && -n "$EXISTING_ADMIN" ]]; then
            USE_EXISTING_ENV=true
            BOT_TOKEN="$EXISTING_TOKEN"
            ADMIN_ID="$EXISTING_ADMIN"
            SUB_BASE_URL="$EXISTING_SUB_URL"

            # Вычисление CADDY_DOMAIN из SUB_BASE_URL
            CADDY_DOMAIN=$(echo "$SUB_BASE_URL" | sed -E 's|^https?://||' | sed -E 's|:[0-9]+.*$||' | sed -E 's|/.*$||')
            if [[ -z "$CADDY_DOMAIN" ]]; then
                if [[ "$SERVER_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
                    CADDY_DOMAIN="${SERVER_IP//./-}.sslip.io"
                else
                    CADDY_DOMAIN="127.0.0.1"
                fi
                SUB_BASE_URL="https://${CADDY_DOMAIN}:8443"
            fi

            cp -f "$ENV_FILE" "$BACKUP_ENV" 2>/dev/null || true

            echo "──────────────────────────────────────────────────────────"
            echo "✅ Обнаружена существующая конфигурация (.env)!"
            echo "   Токен бота:  загружен (***${BOT_TOKEN: -6})"
            echo "   ID админа:   $ADMIN_ID"
            echo "   Подписки:    $SUB_BASE_URL"
            echo "   Хост Caddy:  $CADDY_DOMAIN"
            echo "   (Для полной перенастройки используйте флаг: --reconfigure)"
            echo "──────────────────────────────────────────────────────────"
            echo ""
        fi
    fi
fi

if [[ "$USE_EXISTING_ENV" != true ]]; then
    # ─────────────────────────────────────────────────────────────
    #  ШАГ 1/3: BOT_TOKEN
    # ─────────────────────────────────────────────────────────────
    echo "──────────────────────────────────────────────────────────"
    echo "📌 ШАГ 1/3: Токен Telegram-бота (BOT_TOKEN)"
    echo "   Создайте бота в Telegram через @BotFather (/newbot)"
    echo "   Пример формата: 7123456789:AAHxxxxxxxxxxxxxxxxxxxxxxxx"
    echo "──────────────────────────────────────────────────────────"
    BOT_TOKEN=""
    while true; do
        read -r -p "Введите BOT_TOKEN: " BOT_TOKEN </dev/tty
        BOT_TOKEN=$(echo "$BOT_TOKEN" | tr -d '[:space:]')
        if [[ "$BOT_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]]; then
            break
        else
            echo "❌ Некорректный формат токена! Токен должен иметь вид 123456789:ABC_xyz..."
        fi
    done
    echo "✅ Токен бота принят."
    echo ""

    # ─────────────────────────────────────────────────────────────
    #  ШАГ 2/3: ADMIN_ID
    # ─────────────────────────────────────────────────────────────
    echo "──────────────────────────────────────────────────────────"
    echo "📌 ШАГ 2/3: Числовой ID владельца бота (ADMIN_ID)"
    echo "   Узнать свой ID можно в ботах @userinfobot или @getmyid_bot"
    echo "   Пример формата: 123456789 (только цифры)"
    echo "──────────────────────────────────────────────────────────"
    ADMIN_ID=""
    while true; do
        read -r -p "Введите числовой ADMIN_ID: " ADMIN_ID </dev/tty
        ADMIN_ID=$(echo "$ADMIN_ID" | tr -d '[:space:]')
        if [[ "$ADMIN_ID" =~ ^[0-9]+$ ]]; then
            break
        else
            echo "❌ Некорректный ID! ID владельца должен состоять только из цифр."
        fi
    done
    echo "✅ ID администратора принят: $ADMIN_ID"
    echo ""

    # ─────────────────────────────────────────────────────────────
    #  ШАГ 3/3: BASE_HOST (Домен или IPv4)
    # ─────────────────────────────────────────────────────────────
    echo "──────────────────────────────────────────────────────────"
    echo "📌 ШАГ 3/3: Домен или внешний IP для подписок (BASE_HOST)"
    echo "   • Если у вас есть домен (например, sub.domain.com):"
    echo "     направьте его A-запись на IP: $SERVER_IP и введите его."
    echo "   • Если домена нет:"
    echo "     просто нажмите [Enter], и бот автоматически создаст"
    echo "     бесплатный SSL через ${SERVER_IP//./-}.sslip.io на порту 8443."
    echo "   ⚠️ Порт 443 свободен для VPN (Xray / 3X-UI / WPP)!"
    echo "──────────────────────────────────────────────────────────"
    read -r -p "Введите домен или нажмите Enter [$SERVER_IP]: " BASE_HOST </dev/tty
    BASE_HOST=$(echo "$BASE_HOST" | tr -d '[:space:]')
    if [[ -z "$BASE_HOST" ]]; then
        BASE_HOST="$SERVER_IP"
    fi

    # Вычисление CADDY_DOMAIN и SUB_BASE_URL
    if [[ "$BASE_HOST" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        CADDY_DOMAIN="${BASE_HOST//./-}.sslip.io"
        SUB_BASE_URL="https://${CADDY_DOMAIN}:8443"
    else
        CADDY_DOMAIN="$BASE_HOST"
        SUB_BASE_URL="https://${BASE_HOST}:8443"
    fi
    echo "✅ Базовый хост подписок: $CADDY_DOMAIN"
    echo "✅ Полный SUB_BASE_URL: $SUB_BASE_URL"
    echo ""
fi

# ─────────────────────────────────────────────────────────────
#  [1/4] Установка пакетов и веб-сервера Caddy
# ─────────────────────────────────────────────────────────────
echo "⏳ [1/4] Установка системных пакетов и Caddy..."
if command -v apt-get &>/dev/null; then
    apt-get update -qq
    apt-get install -y git curl python3 python3-venv python3-pip sqlite3 ufw
    if ! command -v caddy &>/dev/null; then
        apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list
        apt-get update -qq && apt-get install -y caddy
    fi
elif command -v yum &>/dev/null; then
    yum install -y git curl python3 python3-venv python3-pip sqlite
    if ! command -v caddy &>/dev/null; then
        yum install -y yum-plugin-copr
        yum copr enable @caddy/caddy -y
        yum install -y caddy
    fi
fi

if command -v ufw &>/dev/null; then
    ufw allow 80/tcp || true
    ufw allow 8443/tcp || true
fi

# ─────────────────────────────────────────────────────────────
#  [2/4] Загрузка / обновление исходного кода Bot_VIP2
# ─────────────────────────────────────────────────────────────
echo "⏳ [2/4] Загрузка исходного кода Bot_VIP2..."
mkdir -p "$BOT_DIR"
if [[ -d "$BOT_DIR/.git" ]]; then
    git -C "$BOT_DIR" pull --ff-only || true
else
    git clone --depth=1 https://github.com/Bandito2025/Bot_VIP2.git "$BOT_DIR"
fi

# ─────────────────────────────────────────────────────────────
#  [3/4] Настройка Python окружения и автомиграция БД
# ─────────────────────────────────────────────────────────────
echo "⏳ [3/4] Настройка Python venv и зависимостей..."
python3 -m venv "$BOT_DIR/venv"
"$BOT_DIR/venv/bin/pip" install --quiet --upgrade pip
"$BOT_DIR/venv/bin/pip" install --quiet -r "$BOT_DIR/requirements.txt"

# Сохранение конфигурации .env (если создаём с нуля или принудительно)
if [[ "$USE_EXISTING_ENV" != true || ! -f "$ENV_FILE" ]]; then
    cat > "$ENV_FILE" << EOF
BOT_TOKEN=${BOT_TOKEN}
ADMIN_ID=${ADMIN_ID}
SUB_PORT=2097
SUB_BASE_URL=${SUB_BASE_URL}
DB_PATH=bot_data.db
CRYPTOBOT_TOKEN=
YUKASSA_SHOP_ID=
YUKASSA_SECRET_KEY=
EOF
fi
chmod 600 "$ENV_FILE"
cp -f "$ENV_FILE" "$BACKUP_ENV" 2>/dev/null || true

# Автомиграция БД
echo "⏳ Запуск автомиграции БД..."
"$BOT_DIR/venv/bin/python" "$BOT_DIR/migrate_db.py"

# ─────────────────────────────────────────────────────────────
#  [4/4] Настройка Caddy (порт 8443) и запуск служб
# ─────────────────────────────────────────────────────────────
echo "⏳ [4/4] Настройка Caddyfile и systemd-сервиса..."
mkdir -p /etc/caddy
cat > /etc/caddy/Caddyfile << EOF
{
    https_port 8443
    servers {
        protocols h1 h2
    }
}

${CADDY_DOMAIN}:8443 {
    reverse_proxy 127.0.0.1:2097
}
EOF

systemctl enable --now caddy
systemctl restart caddy

# Создание глобальной команды быстрого обновления bot-update
cat > /usr/local/bin/bot-update << 'EOF'
#!/usr/bin/env bash
set -e
echo "🔄 Запуск обновления Bot_VIP2..."
cd /opt/Bot_VIP
git pull origin main
/opt/Bot_VIP/venv/bin/python3 migrate_db.py
systemctl restart bot_vip caddy
echo "✅ Bot_VIP2 успешно обновлен и перезапущен!"
EOF
chmod +x /usr/local/bin/bot-update 2>/dev/null || true

cat > /etc/systemd/system/bot_vip.service << EOF
[Unit]
Description=Bot_VIP Telegram VPN Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=${BOT_DIR}
ExecStart=${BOT_DIR}/venv/bin/python bot.py
Restart=always
RestartSec=10
EnvironmentFile=${BOT_DIR}/.env
StandardOutput=append:${BOT_DIR}/bot.log
StandardError=append:${BOT_DIR}/bot.log

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable bot_vip
systemctl restart bot_vip

# ─────────────────────────────────────────────────────────────
#  ФИНАЛЬНАЯ ПАМЯТКА
# ─────────────────────────────────────────────────────────────
echo ""
echo "=========================================================="
echo "  🎉 Установка Bot_VIP2 успешно завершена!"
echo "=========================================================="
echo ""
echo "  📊 Статус служб:"
echo "     • bot_vip:  $(systemctl is-active bot_vip || echo 'running')"
echo "     • caddy:    $(systemctl is-active caddy || echo 'running')"
echo ""
echo "  🌐 URL подписок:  ${SUB_BASE_URL}/sub/<ID>"
echo "  📁 Директория:    ${BOT_DIR}"
echo ""
echo "  📋 3 ШАГА ДЛЯ СТАРТА В TELEGRAM:"
echo "     1. Откройте вашего бота в Telegram и отправьте /start"
echo "     2. Перейдите в «⚙️ Админ-панель» -> «🖥 Сервера» и подключите"
echo "        первый сервер (3X-UI, WPP или Гибрид) через мастер"
echo "     3. В разделе «Оплата и Тарифы» настройте тарифы и платежи"
echo ""
echo "  🛠 ПОЛЕЗНЫЕ КОМАНДЫ:"
echo "     • Быстрое обновление:   bot-update
     • Просмотр логов бота:  journalctl -u bot_vip -f"
echo "     • Перезапуск бота:      systemctl restart bot_vip"
echo "     • Проверка Caddy:       systemctl status caddy"
echo "=========================================================="
