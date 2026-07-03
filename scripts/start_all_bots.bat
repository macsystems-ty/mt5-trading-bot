@echo off
REM start_all_bots.bat
REM Starts all 5 live trading bots AND the Telegram responder.
REM Each bot runs in its own window with auto-restart on crash.

SET PROJECT_DIR=C:\Users\Administrator\mt5-trading-bot
SET DELAY=20

echo ============================================
echo  MT5 Trading Bot Launcher
echo ============================================
echo.

echo Starting bot_1hz25v ...
start "bot_1hz25v" cmd /k "cd /d %PROJECT_DIR% && call run_bot.bat live_bots\1HZ25V\live_mt5_trading_bot.py"
echo   Waiting %DELAY%s ...
timeout /t %DELAY% /nobreak >NUL

echo Starting bot_r100 ...
start "bot_r100" cmd /k "cd /d %PROJECT_DIR% && call run_bot.bat live_bots\R_100\live_mt5_trading_bot.py"
echo   Waiting %DELAY%s ...
timeout /t %DELAY% /nobreak >NUL

echo Starting bot_1hz75v ...
start "bot_1hz75v" cmd /k "cd /d %PROJECT_DIR% && call run_bot.bat live_bots\1HZ75V\live_mt5_trading_bot.py"
echo   Waiting %DELAY%s ...
timeout /t %DELAY% /nobreak >NUL

echo Starting bot_1hz90v ...
start "bot_1hz90v" cmd /k "cd /d %PROJECT_DIR% && call run_bot.bat live_bots\1HZ90V\live_mt5_trading_bot.py"
echo   Waiting %DELAY%s ...
timeout /t %DELAY% /nobreak >NUL

echo Starting bot_1hz100v ...
start "bot_1hz100v" cmd /k "cd /d %PROJECT_DIR% && call run_bot.bat live_bots\1HZ100V\live_mt5_trading_bot.py"
echo   Waiting %DELAY%s ...
timeout /t %DELAY% /nobreak >NUL

echo Starting telegram_responder ...
start "telegram_responder" cmd /k "cd /d %PROJECT_DIR% && call run_bot.bat telegram_responder.py"

echo.
echo ============================================
echo  All bots started!
echo  Check Telegram for BOT STARTED messages.
echo ============================================
