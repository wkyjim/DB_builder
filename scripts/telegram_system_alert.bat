@echo off
setlocal
cd /d "%~dp0.."
set "ALERT_MESSAGE=%*"
if "%ALERT_MESSAGE%"=="" set "ALERT_MESSAGE=Scheduled market data job reported a failure."
"C:\Users\User\anaconda3\envs\PostgreSQL_db\python.exe" scripts\telegram_bot.py --send alert --message "%ALERT_MESSAGE%"
exit /b %ERRORLEVEL%

