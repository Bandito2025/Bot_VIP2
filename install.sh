#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────
#  Bot_VIP — одна команда установки с GitHub
#  Репозиторий: https://github.com/Bandito2025/Bot_VIP2
# ─────────────────────────────────────────────

# 1. Проверка root
[[ $EUID -ne 0 ]] && echo "❌ Запусти от root: sudo bash install.sh" && exit 1

echo "======================================"
echo "  Установка Bot_VIP2 — VPN Telegram Bot"
echo "======================================"

# 2. Определение дистрибутива и установка пакетов
install_packages() {
    if command -v apt-get &>/dev/null; then
        apt-get update -qq
        apt-get install -y git curl python3 python3-venv python3-pip sqlite3
        # Caddy
        if ! command -v caddy &>/dev/null; then
            apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl
            curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
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
    else
        echo "❌ Поддерживаются только apt/yum системы" && exit 1
    fi
}
install_packages

# 3. Запрос параметров (через /dev/tty для работы с curl pipe)
echo ""
echo "Введи параметры установки:"
read -r -p "  BOT_TOKEN (от @BotFather): " BOT_TOKEN </dev/tty
read -r -p "  ADMIN_ID (числовой Telegram ID): " ADMIN_ID </dev/tty
read -r -p "  BASE_HOST (домен или IPv4, Enter = автоопределение): " BASE_HOST </dev/tty

# 4. Автоопределение IP если не указан
if [[ -z "$BASE_HOST" ]]; then
    echo "⏳ Определяю внешний IP..."
    BASE_HOST=$(curl -s4 --max-time 10 ifconfig.me || curl -s4 --max-time 10 api.ipify.org)
    echo "   Найден IP: $BASE_HOST"
fi

# 5. Вычисление домена для Caddy и SUB_BASE_URL
if [[ "$BASE_HOST" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    CADDY_DOMAIN="${BASE_HOST//./-}.sslip.io"
    SUB_BASE_URL="https://${CADDY_DOMAIN}:8443"
else
    CADDY_DOMAIN="$BASE_HOST"
    SUB_BASE_URL="https://${BASE_HOST}:8443"
fi
echo "   Домен Caddy: $CADDY_DOMAIN"
echo "   SUB_BASE_URL: $SUB_BASE_URL"

# 6. Клонирование репозитория
BOT_DIR="/opt/Bot_VIP"
if [[ -d "$BOT_DIR/.git" ]]; then
    echo "⏳ Обновление существующего репозитория..."
    git -C "$BOT_DIR" pull --ff-only
else
    echo "⏳ Клонирование репозитория..."
    git clone --depth=1 https://github.com/Bandito2025/Bot_VIP2.git "$BOT_DIR"
fi

# 7. Python venv и зависимости
echo "⏳ Установка Python-зависимостей..."
python3 -m venv "$BOT_DIR/venv"
"$BOT_DIR/venv/bin/pip" install --quiet --upgrade pip
"$BOT_DIR/venv/bin/pip" install --quiet -r "$BOT_DIR/requirements.txt"

# 8. Создание .env
cat > "$BOT_DIR/.env" << EOF
BOT_TOKEN=${BOT_TOKEN}
ADMIN_ID=${ADMIN_ID}
SUB_PORT=2097
SUB_BASE_URL=${SUB_BASE_URL}
DB_PATH=bot_data.db
CRYPTOBOT_TOKEN=
YUKASSA_SHOP_ID=
YUKASSA_SECRET_KEY=
EOF
chmod 600 "$BOT_DIR/.env"
echo "✅ .env создан"

# 9. Настройка Caddy (порт 8443 — НЕ 443, он для VPN)
cat > /etc/caddy/Caddyfile << EOF
{
    https_port 8443
}

${CADDY_DOMAIN} {
    reverse_proxy 127.0.0.1:2097
}
EOF
systemctl enable --now caddy
systemctl reload caddy || systemctl restart caddy
echo "✅ Caddy настроен на ${CADDY_DOMAIN}:8443"

# 10. Systemd юнит для бота
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
echo "✅ Бот запущен как systemd-сервис bot_vip"

# 11. Итоговое сообщение
echo ""
echo "════════════════════════════════════════════"
echo "  ✅ Bot_VIP2 установлен успешно!"
echo "════════════════════════════════════════════"
echo ""
echo "  🌐 URL подписок:  ${SUB_BASE_URL}/sub/SUBSCRIPTION_ID"
echo "  📋 Логи бота:     journalctl -u bot_vip -f"
echo "  🔧 Управление:    systemctl restart bot_vip"
echo ""
echo "  ⚡ Следующий шаг: открой Telegram-бота и"
echo "     через меню администратора добавь серверы"
echo "     3X-UI / WPP через мастер настройки."
echo ""
echo "  ⚠️  Порт 443 — ТОЛЬКО для VPN (Xray/3X-UI/WPP)!"
echo "     Caddy использует порт 8443 для подписок."
echo "════════════════════════════════════════════"
