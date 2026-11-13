@echo off
chcp 65001 >nul
title Bot_VIP
cd /d "%~dp0"
echo ===================================================
echo   Запуск Telegram-бота Bot_VIP...
echo ===================================================
python bot.py
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [!] Ошибка запуска бота (Код ошибки: %ERRORLEVEL%).
    pause
)
