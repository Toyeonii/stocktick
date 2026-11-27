@echo off
rem ============================================================
rem  StockTick release build  ->  .release\StockTick.exe
rem  (messages are in English on purpose: Korean text in .bat files
rem   can break depending on the file encoding / code page)
rem ============================================================
cd /d "%~dp0"

set PY=C:\Users\yongju.lee\AppData\Local\Programs\Python\Python312\python.exe
if not exist "%PY%" set PY=python

echo [1/4] Installing build tools (pyinstaller, pillow for the icon)...
"%PY%" -m pip install --quiet --upgrade pyinstaller pillow
if errorlevel 1 goto :fail

echo [2/4] Building StockTick.exe (takes about 1 minute)...
"%PY%" -m PyInstaller --noconfirm --clean --onefile --noconsole --name StockTick ^
  --icon "static\icon-512.png" ^
  --add-data "index.html;." ^
  --add-data "manifest.json;." ^
  --add-data "sw.js;." ^
  --add-data "stock_list.json;." ^
  --add-data "static;static" ^
  app.py
if errorlevel 1 goto :fail

echo [3/4] Copying files to .release ...
if not exist ".release" mkdir ".release"
copy /y "dist\StockTick.exe" ".release\" >nul
copy /y ".env.example" ".release\" >nul
copy /y "RELEASE_README.txt" ".release\README.txt" >nul

if exist ".env" (
  choice /c YN /m "Copy your .env (KIS app key/secret) into .release"
  if not errorlevel 2 copy /y ".env" ".release\" >nul
)
if exist "watchlists.json" (
  choice /c YN /m "Copy your watchlists.json (watchlist groups) into .release"
  if not errorlevel 2 copy /y "watchlists.json" ".release\" >nul
)

echo [4/4] Cleaning up build files...
rmdir /s /q build 2>nul
rmdir /s /q dist 2>nul
del /q StockTick.spec 2>nul

echo.
echo Done. Files in .release:
dir /b ".release"
for %%F in (".release\StockTick.exe") do echo StockTick.exe size: %%~zF bytes
pause
exit /b 0

:fail
echo.
echo BUILD FAILED. Please send the messages above.
pause
exit /b 1
