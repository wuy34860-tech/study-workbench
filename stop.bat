@echo off
chcp 65001 >nul
title Learning Workbench (stop)
echo ============================================
echo   Stopping Learning Workbench
echo ============================================
echo.

set "KILLED=0"

rem kill whatever is listening on 5000
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5000" ^| findstr LISTENING') do (
  echo Found PID=%%a on port 5000
  taskkill /F /PID %%a >nul 2>&1
  if not errorlevel 1 set "KILLED=1"
)

if "%KILLED%"=="1" (
  echo.
  echo OK, stopped.
) else (
  echo (port 5000 already free)
)
echo.
pause
exit /b