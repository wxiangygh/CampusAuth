@echo off
chcp 65001 >nul
echo === netsh wlan show interfaces ===
netsh wlan show interfaces
echo.
echo === netsh interface ipv4 show interfaces ===
netsh interface ipv4 show interfaces
echo.
pause
