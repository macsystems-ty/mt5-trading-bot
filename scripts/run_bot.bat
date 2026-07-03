@echo off
REM run_bot.bat - restarts a bot automatically on crash
REM Usage: call run_bot.bat <script_path>

REM Fix SSL certificate for Telegram
FOR /F "tokens=*" %%i IN ('python -c "import certifi; print(certifi.where())"') DO SET SSL_CERT_FILE=%%i
SET REQUESTS_CA_BUNDLE=%SSL_CERT_FILE%

:loop
echo [%date% %time%] Starting %1 ...
python %1
echo [%date% %time%] Bot exited. Restarting in 15 seconds...
timeout /t 15 /nobreak >NUL
goto loop
