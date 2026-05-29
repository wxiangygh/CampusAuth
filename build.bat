@echo off
echo ============================================================
echo            校园网助手 - 打包工具
echo ============================================================
echo.

:: Check if PyInstaller is installed
python -c "import PyInstaller" 2>nul
if %errorlevel% neq 0 (
    echo Installing PyInstaller...
    pip install pyinstaller
)

:: Clean up previous builds
if exist "build" rd /s /q "build"
if exist "dist" rd /s /q "dist"
if exist "*.spec" del /q "*.spec"

:: Build with PyInstaller
echo Building WiFi Auto-Auth Tray App...
pyinstaller --onefile --windowed --name "校园网助手" --icon=NONE --add-data "settings.html;." --add-data "config.py;." --hidden-import webview --hidden-import PIL --hidden-import pystray --hidden-import ctypes tray_app.py

if exist "dist\校园网助手.exe" (
    echo.
    echo ============================================================
    echo  Build successful!
    echo  Output: dist\校园网助手.exe
    echo ============================================================
) else (
    echo.
    echo Build failed!
)

pause
