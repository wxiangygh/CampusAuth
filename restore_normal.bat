@echo off
:: Restore network to normal mode: disconnect WARP, enable IPv4

:: Check admin rights
net session >nul 2>&1
if %errorlevel% == 0 (
    echo [Admin] Running restore script...
    goto :run_script
)

:: Auto-elevate
echo Requesting admin rights...
start "" /wait powershell -Command "Start-Process '%~f0' -Verb RunAs"
exit /b

:run_script
echo ============================================================
echo            恢复网络到正常模式
echo ============================================================

cd /d %~dp0

:: Run Python restore script
python auto_wifi_login.py --restore

echo.
echo Press any key to exit...
pause >nul
exit /b
