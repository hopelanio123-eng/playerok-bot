@echo off
chcp 65001 >nul
setlocal
title TG Project - Tray Launcher
cd /d "%~dp0"

set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"

echo ============================================================
echo   TG PROJECT - launcher with system tray icon
echo ============================================================
echo.

rem --- find python ---
set "PY="
where py >nul 2>nul && set "PY=py -3"
if defined PY goto python_found
where python >nul 2>nul && set "PY=python"
if defined PY goto python_found

echo [X] Python not found in PATH.
echo     Install Python 3.10+ from python.org and try again.
echo.
pause
exit /b 1

:python_found
echo [*] Interpreter: %PY%
%PY% -c "import sys; print('    Python', sys.version.split()[0])"
echo.

rem --- dependencies ---
%PY% -c "import aiogram, fastapi, uvicorn, httpx, aiohttp, requests, bs4, pydantic" >nul 2>nul
if not errorlevel 1 goto tray_libs
echo [i] Missing libraries. Installing from requirements.txt...
%PY% -m pip install --upgrade pip
%PY% -m pip install -r requirements.txt
if errorlevel 1 goto install_failed
echo.

rem --- tray libs ---
:tray_libs
%PY% -c "import pystray, PIL, tkinter" >nul 2>nul
if not errorlevel 1 goto check_running
echo [i] Installing tray libraries (pystray / pillow)...
%PY% -m pip install pystray pillow
if errorlevel 1 goto install_failed
echo.

:install_failed
echo.
echo [X] Dependency install failed. Check your internet and retry.
pause
exit /b 1

rem --- controller already running? ---
:check_running
%PY% -c "import socket,sys; s=socket.socket(); r=s.connect_ex(('127.0.0.1',9324)); s.close(); sys.exit(0 if r==0 else 1)" >nul 2>nul
if not errorlevel 1 goto already_running
echo [*] Starting controller and tray icon...
goto runner

:already_running
echo [i] Controller is already running on :9324 - attaching to it.
echo.

:runner
echo [*] Tray icon appears in the notification area (near the clock).
echo [*] Right-click the icon: Status / Show logs / Restart / Modes / Exit
echo [*] Telegram commands: /mode   /mode all^|funpay^|playerok^|watcher^|off   /bots
echo.

rem --- run without console window if possible ---
set "RUNNER=%PY%"
set "PYWEXE="
if "%~1"=="--console" goto run
for /f "delims=" %%i in ('where pythonw 2^>nul') do set "PYWEXE=%%i"
if not defined PYWEXE goto run
set "RUNNER=%PYWEXE%"

:run
rem Запускаем трей-лаунчер отдельным процессом: это окно cmd сразу закроется,
rem а иконка останется в трее. Ошибки пишем в tray_error.log / tray.log.
start "" /b %RUNNER% tray_launcher.py >nul 2>nul

rem Даём трею подняться и проверяем, что контроллер отвечает
rem (ping вместо timeout - работает и без консоли)
ping -n 26 127.0.0.1 >nul
%PY% -c "import socket,sys; s=socket.socket(); r=s.connect_ex(('127.0.0.1',9324)); s.close(); sys.exit(0 if r==0 else 1)" >nul 2>nul
if not errorlevel 1 goto tray_ok

echo.
echo [!] Controller is not answering yet. Check tray.log / tray_error.log
echo     The tray icon should be in the notification area.
ping -n 4 127.0.0.1 >nul
endlocal
exit /b 0

:tray_ok
echo.
echo [+] Tray icon is running (notification area, near the clock).
echo     Right-click the icon - Status / Show logs / Restart / Modes / Exit
echo     Telegram commands: /mode  /settings  /bots
echo.
echo     This window closes now. To stop everything - right-click the tray icon, Exit.
ping -n 5 127.0.0.1 >nul
endlocal
