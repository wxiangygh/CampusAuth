@echo off
chcp 65001 >nul
echo 正在启动校园网自动认证脚本...
cd /d %~dp0
python auto_wifi_login.py
pause
