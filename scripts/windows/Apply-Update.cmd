@echo off
setlocal
set "PW_MANUAL_UPDATE_DIR=%~dp0"
powershell.exe -NoProfile -Command "& { $ErrorActionPreference='Stop'; try { $root=(Get-ItemProperty -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\ProjectWatchDesktop' -ErrorAction Stop).InstallLocation } catch { $root=Read-Host 'ProjectWatch installation folder' }; if ([string]::IsNullOrWhiteSpace($root)) { exit 1 }; Write-Host ('Update installation: '+$root); Write-Host 'Exit ProjectWatch from its tray menu first. Verify the update ZIP SHA-256 against the official release. Local modifications will block the update.'; if ((Read-Host 'Apply this update? Type UPDATE to confirm') -cne 'UPDATE') { exit 1 }; $state=Get-Content -Raw -LiteralPath (Join-Path $root 'install-state.json') | ConvertFrom-Json; if ($state.active -notmatch '^[0-9a-f]{64}$') { throw 'Invalid install identity' }; $python=Join-Path $root ('versions/'+$state.active+'/runtime/python.exe'); & $python -I -B -X utf8 (Join-Path $env:PW_MANUAL_UPDATE_DIR 'apply_update.py') --root $root --confirm; exit $LASTEXITCODE }"
set "PW_UPDATE_EXIT=%ERRORLEVEL%"
if not "%PW_UPDATE_EXIT%"=="0" echo Update stopped. Existing files and failure staging are preserved. Do not delete your data or retry model requests.
pause
exit /b %PW_UPDATE_EXIT%
