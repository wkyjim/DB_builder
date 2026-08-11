@echo off
setlocal
cd /d "%~dp0.."
"C:\Users\User\anaconda3\envs\PostgreSQL_db\python.exe" scripts\telegram_bot.py --send morning
exit /b %ERRORLEVEL%

