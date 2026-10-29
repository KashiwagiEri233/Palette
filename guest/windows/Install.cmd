@echo off
setlocal
reg query HKLM\SYSTEM\CurrentControlSet\Control\MiniNT >nul 2>&1
if not errorlevel 1 (
  echo Windows Setup: choose Load driver and browse to drivers on this disc.
  echo Disk: viostor or vioscsi. Network: NetKVM. Select your OS and amd64.
  echo Run Install.cmd after Windows installation and login.
  pause
  exit /b 1
)
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install.ps1" %*
set "result=%errorlevel%"
echo.
pause
exit /b %result%
