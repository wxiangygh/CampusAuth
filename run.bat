@echo off
:: No process check needed - Python script handles single-instance via mutex

:: Check admin rights
net session >nul 2>&1
if %errorlevel% == 0 (
    echo [Admin] Running script...
    goto :run_script
)

:: Auto-elevate
echo Requesting admin rights...
start "" /wait powershell -Command "Start-Process '%~f0' -Verb RunAs"
exit /b

:run_script
echo ============================================================
echo            WiFi Auto-Auth Script v1.0 (Admin Mode)
echo ============================================================

cd /d %~dp0

:: Run Python script and capture exit code
python auto_wifi_login.py
set "pyExitCode=%errorlevel%"

:: Check Python exit code
if %pyExitCode% == 0 (
    echo.
    echo [SUCCESS] All steps completed successfully.
    echo Closing in 3 seconds...
    timeout /t 3 >nul
    exit /b
) else (
    echo.
    echo [FAILED] Script failed with error code %pyExitCode%.
    echo ============================================================
    echo Press any key to retry...
    echo ============================================================
    pause >nul
    :: Restart script
    start "" "%~f0"
    exit /b
)
