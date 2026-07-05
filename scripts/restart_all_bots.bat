@echo off
REM restart_all_bots.bat - kills all bots and restarts them cleanly

echo ============================================
echo  Stopping all running bots...
echo ============================================

taskkill /F /IM python.exe >NUL 2>&1
echo All Python processes stopped.

REM Clear all flag files so start_all_bots.bat starts fresh
if exist "bot_flags" (
    del /Q "bot_flags\*.running" >NUL 2>&1
    echo Flag files cleared.
)

timeout /t 5 /nobreak >NUL

echo ============================================
echo  Starting all bots...
echo ============================================
call start_all_bots.bat
