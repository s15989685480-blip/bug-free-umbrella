@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>&1
if %errorlevel%==0 (
  set "PYTHON=py"
) else (
  set "PYTHON=python"
)

start "考勤同步服务" /min "%PYTHON%" "%~dp0sync_server.py" --port 8765
timeout /t 1 /nobreak >nul
for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "(Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -like '10.*' -or $_.IPAddress -like '192.168.*' -or $_.IPAddress -like '172.*' } | Select-Object -First 1).IPAddress"`) do set "LAN_IP=%%i"
if defined LAN_IP (
  start "" "http://%LAN_IP%:8765/attendance.html"
) else (
  start "" "http://127.0.0.1:8765/attendance.html"
)

endlocal
