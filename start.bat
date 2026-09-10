@echo off
chcp 65001 >nul
title Learning Workbench
cd /d "%~dp0"

rem ===== paths =====
set "PY=.venv\Scripts\python.exe"
set "URL=http://127.0.0.1:5000"

rem ===== first-run: create venv and install deps =====
if not exist "%PY%" (
  echo First run: creating venv and installing deps...
  where python >nul 2>&1
  if errorlevel 1 (
    echo ERROR: python not found. Please install Python 3.10+ and add to PATH.
    pause & exit /b 1
  )
  python -m venv .venv || (pause & exit /b 1)
  "%PY%" -m pip install -r requirements.txt || (pause & exit /b 1)
)

rem ===== check port 5000 =====
set "PORT_PID="
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5000" ^| findstr LISTENING') do (
  set "PORT_PID=%%a"
  goto :check_done
)
:check_done

if defined PORT_PID (
  echo ============================================
  echo   Port 5000 is busy (PID = %PORT_PID%)
  echo ============================================
  echo   Killing it in 3s, then restarting.
  echo   (no input is read; close window to abort)
  echo.
  ping -n 4 127.0.0.1 >nul
  echo Killing PID %PORT_PID% ...
  taskkill /F /PID %PORT_PID% >nul 2>&1
  ping -n 2 127.0.0.1 >nul
)

rem ===== launch Flask via VBScript (hidden, detached) =====
wscript start_hidden.vbs
ping -n 4 127.0.0.1 >nul

rem ===== open browser =====
start "" "%URL%"

echo.
echo ============================================
echo   Learning Workbench is running.
echo.
echo   URL:      %URL%
echo   To stop:  run stop.bat
echo   Closing this window will NOT stop the server.
echo ============================================
echo.
echo (window auto-closes in 8s; Ctrl+C to close now)
ping -n 9 127.0.0.1 >nul
exit /b