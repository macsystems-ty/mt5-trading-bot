@echo off
REM run_bot.bat - watchdog wrapper for a single bot
REM Usage: run_bot.bat <script>

SET BOT_SCRIPT=%~1
SET PYTHONIOENCODING=utf-8

FOR /F "tokens=*" %%i IN ('python -c "import certifi; print(certifi.where())"') DO SET SSL_CERT_FILE=%%i
SET REQUESTS_CA_BUNDLE=%SSL_CERT_FILE%

:loop
echo [%date% %time%] Starting %BOT_SCRIPT% ...
python %BOT_SCRIPT%
echo [%date% %time%] Bot exited. Restarting in 15 seconds...
timeout /t 15 /nobreak >NUL
goto loop
