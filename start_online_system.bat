@echo off
cd /d "%~dp0"

set "URL=http://127.0.0.1:8020/"
set "HEALTH=http://127.0.0.1:8020/api/health"

powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Invoke-WebRequest -Uri '%HEALTH%' -UseBasicParsing -TimeoutSec 2 | Out-Null; Start-Process '%URL%'; exit 0 } catch { exit 1 }"
if %errorlevel%==0 exit /b 0

if exist "D:\anaconda\python.exe" (
  set "PYTHON_EXE=D:\anaconda\python.exe"
) else (
  set "PYTHON_EXE=python"
)

start "TNBC Online Backend" "%PYTHON_EXE%" -B "%~dp0scripts\serve_online_system.py" --host 127.0.0.1 --port 8020
timeout /t 2 >nul
start "" "%URL%"
