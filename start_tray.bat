@echo off
:: Start the WiFi Auto-Auth tray application

cd /d %~dp0

:: Check if pystray is installed
python -c "import pystray" 2>nul
if %errorlevel% neq 0 (
    echo Installing dependencies...
    pip install pystray pillow
)

:: Start the tray app (minimized)
start /min pythonw tray_app.py
exit /b
