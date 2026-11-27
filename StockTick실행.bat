@echo off
cd /d "%~dp0"
set PY=C:\Users\yongju.lee\AppData\Local\Programs\Python\Python312\python.exe
set WHALE=C:\Program Files\Naver\Naver Whale\Application\whale.exe
set PROFILE=%~dp0whale-profile

start "StockTick 서버" "%PY%" app.py
timeout /t 3 >nul
start "" "%WHALE%" --app=http://localhost:5000 --window-size=1536,1112 --user-data-dir="%PROFILE%"