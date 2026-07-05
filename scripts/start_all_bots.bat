@echo off
REM start_all_bots.bat
REM Starts all 11 bots. If already 11 Python processes running, skips all.

SET PROJECT_DIR=C:\Users\Administrator\mt5-trading-bot
SET DELAY=20
SET EXPECTED_BOTS=11

REM Set SSL certificate and UTF-8
FOR /F "tokens=*" %%i IN ('python -c "import certifi; print(certifi.where())"') DO SET SSL_CERT_FILE=%%i
SET REQUESTS_CA_BUNDLE=%SSL_CERT_FILE%
SET PYTHONIOENCODING=utf-8

echo ============================================
echo  MT5 Trading Bot Launcher
echo ============================================
echo.

REM Count running Python processes
FOR /F %%i IN ('tasklist /FI "IMAGENAME eq python.exe" /FO CSV ^| find /C "python.exe"') DO SET RUNNING=%%i

echo Currently running Python processes: %RUNNING%

IF %RUNNING% GEQ %EXPECTED_BOTS% (
    echo All %EXPECTED_BOTS% bots already running. Nothing to do.
    echo Use restart_all_bots.bat to force a restart.
    goto :EOF
)

echo Starting bots...
echo.

REM ── Main Bots ────────────────────────────────────────────────────────────

call :START_BOT "bot_1hz25v" "live_bots\1HZ25V\live_mt5_trading_bot.py"
timeout /t %DELAY% /nobreak >NUL

call :START_BOT "bot_r100" "live_bots\R_100\live_mt5_trading_bot.py"
timeout /t %DELAY% /nobreak >NUL

call :START_BOT "bot_1hz75v" "live_bots\1HZ75V\live_mt5_trading_bot.py"
timeout /t %DELAY% /nobreak >NUL

call :START_BOT "bot_1hz90v" "live_bots\1HZ90V\live_mt5_trading_bot.py"
timeout /t %DELAY% /nobreak >NUL

call :START_BOT "bot_1hz100v" "live_bots\1HZ100V\live_mt5_trading_bot.py"
timeout /t %DELAY% /nobreak >NUL

REM ── Test Bots ────────────────────────────────────────────────────────────

call :START_BOT "test_1hz25v" "live_bots\1HZ25V\live_mt5_test_bot_v25.py"
timeout /t %DELAY% /nobreak >NUL

call :START_BOT "test_1hz75v" "live_bots\1HZ75V\live_mt5_test_bot_1hz75v.py"
timeout /t %DELAY% /nobreak >NUL

call :START_BOT "test_1hz90v" "live_bots\1HZ90V\live_mt5_test_bot_1hz90v.py"
timeout /t %DELAY% /nobreak >NUL

call :START_BOT "test_1hz100v" "live_bots\1HZ100V\live_mt5_test_bot_1hz100v.py"
timeout /t %DELAY% /nobreak >NUL

call :START_BOT "test_r100" "live_bots\R_100\live_mt5_test_bot_r_100.py"
timeout /t %DELAY% /nobreak >NUL

REM ── Telegram Responder ───────────────────────────────────────────────────

call :START_BOT "telegram_resp" "telegram_responder.py"

echo.
echo ============================================
echo  Done! Active Python processes:
echo ============================================
tasklist /FI "IMAGENAME eq python.exe" /FO CSV | find /C "python.exe"
goto :EOF


REM ── Subroutine ───────────────────────────────────────────────────────────
:START_BOT
SET BOT_NAME=%~1
SET BOT_SCRIPT=%~2
echo Starting %BOT_NAME% ^(%BOT_SCRIPT%^) ...
start "%BOT_NAME%" cmd /k "SET SSL_CERT_FILE=%SSL_CERT_FILE%&& SET REQUESTS_CA_BUNDLE=%SSL_CERT_FILE%&& SET PYTHONIOENCODING=utf-8&& cd /d %PROJECT_DIR%&& python %BOT_SCRIPT%"
echo   Started!
goto :EOF
