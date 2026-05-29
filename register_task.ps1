# Register WiFi Auto-Auth Task (Interactive Mode)

chcp 65001 > $null
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$adminCheck = [bool]([System.Security.Principal.WindowsIdentity]::GetCurrent().Groups -match "S-1-5-32-544")
if (-not $adminCheck) {
    Write-Host "Need admin rights..." -ForegroundColor Yellow
    Start-Process powershell.exe -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -Command `"chcp 65001; [Console]::OutputEncoding=[System.Text.Encoding]::UTF8; & '$PSCommandPath'`""
    exit
}

$wifiName = "CMCC_BJUT_SUSHE_H1010-5G"
$configPath = Join-Path $PSScriptRoot "config.py"
if (Test-Path $configPath) {
    $content = Get-Content $configPath -Raw
    if ($content -match 'WIFI_NAME\s*=\s*["''](.+?)["'']') {
        $wifiName = $Matches[1]
    }
}

$scriptPath = Join-Path $PSScriptRoot "run.bat"
$taskName = "CMCC_AutoAuth"
$xmlFile = Join-Path $PSScriptRoot "_task_def.xml"

Write-Host "============================================================"
Write-Host "        Register WiFi Auto-Auth (Interactive Mode)"
Write-Host "============================================================"
Write-Host ""
Write-Host ("WiFi: " + $wifiName)
Write-Host ("Script: " + $scriptPath)
Write-Host ""

# Delete old task
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

# Use EventID 8001 with interactive user (not SYSTEM) so UAC can show
[xml]$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Auto auth when connecting to $($wifiName)</Description>
  </RegistrationInfo>
  <Triggers>
    <EventTrigger>
      <Enabled>true</Enabled>
      <Subscription>&lt;QueryList&gt;&lt;Query Id="0" Path="Microsoft-Windows-WLAN-AutoConfig/Operational"&gt;&lt;Select Path="Microsoft-Windows-WLAN-AutoConfig/Operational"&gt;*[System[(EventID=8001)]]&lt;/Select&gt;&lt;/Query&gt;&lt;/QueryList&gt;</Subscription>
    </EventTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>cmd.exe</Command>
      <Arguments>/c start "" "$scriptPath"</Arguments>
    </Exec>
  </Actions>
</Task>
"@

$xml.Save($xmlFile)
Write-Host ("XML: " + $xmlFile)

$cmd = 'schtasks /create /tn "' + $taskName + '" /xml "' + $xmlFile + '" /f'
Write-Host ("Cmd: " + $cmd)

$result = cmd.exe /c $cmd 2>&1
Write-Host $result

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "[OK] Task registered with interactive user!"
    Write-Host ("Will trigger when connecting to any WiFi")
    Write-Host ("Script will check if it's: " + $wifiName)
    Write-Host ""
    Write-Host "Delete: schtasks /delete /tn '" + $taskName + "' /f"
    Write-Host "Test: schtasks /run /tn '" + $taskName + "'"
} else {
    Write-Host ""
    Write-Host "[FAIL] Could not create task"
}

Remove-Item $xmlFile -ErrorAction SilentlyContinue
Write-Host ""
Write-Host "Press any key to exit..."
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
