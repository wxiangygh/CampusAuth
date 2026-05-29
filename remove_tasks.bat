@echo off
chcp 65001 >nul
echo ============================================================
echo        Delete All WiFi Auto-Auth Tasks
echo ============================================================
echo.

schtasks /delete /tn "CMCC_AutoAuth" /f 2>nul
if %errorlevel% == 0 (
    echo [OK] Deleted: CMCC_AutoAuth
) else (
    echo [Not Found] CMCC_AutoAuth
)

schtasks /delete /tn "WiFi_Test_Hook" /f 2>nul
if %errorlevel% == 0 (
    echo [OK] Deleted: WiFi_Test_Hook
) else (
    echo [Not Found] WiFi_Test_Hook
)

echo.
echo All tasks removed.
echo.
pause
