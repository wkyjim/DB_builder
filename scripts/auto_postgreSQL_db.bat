@echo off
:: 1. 初始化 Conda 設定 (請根據你的 Anaconda 安裝路徑修改)
:: 通常路徑在 C:\Users\你的用戶名\anaconda3\Scripts\activate.bat
call "C:\Users\User\anaconda3\Scripts\activate.bat"

:: 2. 啟動你的環境 (假設名稱是 base)
call conda activate PostgreSQL_db

:: 3. 執行你的 py 檔
cd /d "C:\Users\User\OneDrive\Coding\DB_builder"

python scripts\pgSQL_equities_auto.py

pause
