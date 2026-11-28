@echo off
cd /d "%~dp0"
set PY=C:\Users\yeonj\AppData\Local\Programs\Python\Python312\python.exe
if not exist "%PY%" set PY=python
set WHALE=C:\Program Files\Naver\Naver Whale\Application\whale.exe

start "StockTick server" "%PY%" app.py
timeout /t 3 >nul
if exist "%WHALE%" (
    start "" "%WHALE%" --app=http://localhost:5000 --start-maximized --user-data-dir="%~dp0whale-profile"
) else (
  start "" http://localhost:5000
)