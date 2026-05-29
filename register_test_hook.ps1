# Register simple test hook for WiFi connection

chcp 65001 > $null
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$adminCheck = [bool]([System.Security.Principal.WindowsIdentity]::GetCurrent().Groups -match "S-1-5-32-544")
if (-not $adminCheck) {
    Write-Host "Need admin rights..." -ForegroundColor Yellow
    Start-Process powershell.exe -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -Command `"chcp 65001; [Console]::OutputEncoding=[System.Text.Encoding]::UTF8; & '$PSCommandPath'`""
    exit
}

$hookPath = Join-Path $PSScriptRoot "test_hook.bat"
$taskName = "WiFi_Test_Hook"
$xmlFile = Join-Path $PSScriptRoot "_hook_task.xml"

Write-Host "============================================================"
Write-Host "        Register WiFi Test Hook (EventID 8001)"
Write-Host "============================================================"
Write-Host ""
Write-Host ("Hook script: " + $hookPath)
Write-Host ""

# Delete old task
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

# Create simple event trigger task
[xml]$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Test hook for WiFi connection event</Description>
  </RegistrationInfo>
  <Triggers>
    <EventTrigger>
      <Enabled>true</Enabled>
      <Subscription>&lt;QueryList&gt;&lt;Query Id="0" Path="Microsoft-Windows-WLAN-AutoConfig/Operational"&gt;&lt;Select Path="Microsoft-Windows-WLAN-AutoConfig/Operational"&gt;*[System[(EventID=8001)]]&lt;/Select&gt;&lt;/Query&gt;&lt;/QueryList&gt;</Subscription>
    </EventTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>S-1-5-18</UserId>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>false</AllowHardTerminate>
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
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>cmd.exe</Command>
      <Arguments>/c "$hookPath"</Arguments>
    </Exec>
  </Actions>
</Task>
"@

$xml.Save($xmlFile)

$cmd = 'schtasks /create /tn "' + $taskName + '" /xml "' + $xmlFile + '" /f'
Write-Host ("Cmd: " + $cmd)

$result = cmd.exe /c $cmd 2>&1
Write-Host $result

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "[OK] Test hook registered!"
    Write-Host ""
    Write-Host "Test steps:"
    Write-Host "1. Disconnect from WiFi"
    Write-Host "2. Reconnect to WiFi"
    Write-Host "3. Check log file: D:\project_code\ipv6\wifi_hook.log"
    Write-Host ""
    Write-Host "Manual test: schtasks /run /tn '" + $taskName + "'"
    Write-Host "Delete: schtasks /delete /tn '" + $taskName + "' /f"
} else {
    Write-Host ""
    Write-Host "[FAIL] Could not create task"
}

Remove-Item $xmlFile -ErrorAction SilentlyContinue
Write-Host ""
Write-Host "Press any key to exit..."
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
