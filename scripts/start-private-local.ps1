param(
    [string]$Distro = 'Ubuntu-24.04',
    [string]$ServiceUser = 'pwparser',
    [int]$Port = 8796,
    [switch]$NoBrowser
)
$ErrorActionPreference = 'Stop'
# Run after installing the wheel, rootless engine and project-watch user unit.
# Supported WSL setting keeps systemd services alive without a sleeping client.
# Applies to the Windows user's distributions, not only this pilot.
if ($Distro -notmatch '^[A-Za-z0-9._-]+$' -or $ServiceUser -notmatch '^[a-z_][a-z0-9_-]*$') {
    throw 'Invalid distro or service user'
}
if ($Port -lt 1 -or $Port -gt 65535) { throw 'Invalid port' }
$configPath = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.wslconfig'
$hasPersistence = $false
$configSection = ''
if (Test-Path -LiteralPath $configPath) {
    foreach ($line in Get-Content -LiteralPath $configPath) {
        if ($line -match '^\s*\[([^]]+)\]\s*(?:[#;].*)?$') { $configSection = $Matches[1] }
        elseif ($configSection -eq 'general' -and $line -match '^\s*instanceIdleTimeout\s*=\s*(-?\d+)\s*(?:[#;].*)?$') {
            $hasPersistence = $Matches[1] -eq '-1'
        }
    }
}
if (-not $hasPersistence) {
    throw "Set [general] instanceIdleTimeout=-1 in $configPath before using the persistent pilot."
}
$url = "http://127.0.0.1:$Port"
$runtime = $null
try { $runtime = Invoke-RestMethod "$url/api/runtime" -TimeoutSec 3 } catch { }
if ($null -ne $runtime -and -not $runtime.private_local_intake) {
    throw "Port $Port is serving a different mode. Stop that service explicitly before starting the private pilot."
}
if ($null -eq $runtime) {
    $serviceUid = (& wsl.exe --distribution $Distro --user $ServiceUser --exec id -u).Trim()
    if ($LASTEXITCODE -ne 0 -or $serviceUid -notmatch '^\d+$') { throw 'Could not identify the Linux service user' }
    & wsl.exe --distribution $Distro --user $ServiceUser --exec env "XDG_RUNTIME_DIR=/run/user/$serviceUid" systemctl --user start project-watch
    if ($LASTEXITCODE -ne 0) { throw 'The private service could not start; inspect its systemd journal.' }
}
if ($null -eq $runtime) {
    $deadline = (Get-Date).AddSeconds(45)
    do {
        Start-Sleep -Milliseconds 750
        try { $runtime = Invoke-RestMethod "$url/api/runtime" -TimeoutSec 2 } catch { }
    } until ($runtime -or (Get-Date) -gt $deadline)
}
if (-not $runtime.private_local_intake -or $runtime.parser -ne 'container') {
    throw 'Private pilot did not become ready. Inspect: wsl -d Ubuntu-24.04 -u pwparser --exec journalctl --user -u project-watch -n 30'
}
Write-Output "Private local pilot ready: $url"
if (-not $NoBrowser) { Start-Process $url }
