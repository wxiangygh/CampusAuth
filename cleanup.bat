@echo off
echo Cleaning up lock files...
del "%TEMP%\wifi_auth.lock" 2>nul
del "%TEMP%\wifi_auth.time" 2>nul
echo Done.
pause
