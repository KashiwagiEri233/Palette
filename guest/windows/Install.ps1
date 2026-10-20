# Runs inside the Windows guest only. No host execution or Windows setup bypasses.
$ErrorActionPreference = 'Stop'
try {
    $architecture = $env:PROCESSOR_ARCHITECTURE
    if ($env:PROCESSOR_ARCHITEW6432) { $architecture = $env:PROCESSOR_ARCHITEW6432 }
    if ($architecture -ne 'AMD64') { throw 'This disc supports Windows x64 only. ARM64 and x86 are not supported.' }
    if ([Environment]::OSVersion.Version.Major -lt 10) { throw 'Windows 10 / Server 2016 or newer is required.' }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        $arguments = '-NoLogo -NoProfile -ExecutionPolicy Bypass -File "' + $PSCommandPath + '"'
        $elevated = Start-Process -FilePath 'powershell.exe' -ArgumentList $arguments -Verb RunAs -Wait -PassThru
        exit $elevated.ExitCode
    }
    $logs = Join-Path $env:ProgramData 'Palette\GuestTools\Logs'
    New-Item -ItemType Directory -Force -Path $logs | Out-Null
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    Start-Transcript -Path (Join-Path $logs ($stamp + '-install.log')) | Out-Null
    $manifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'payloads.json') -Raw | ConvertFrom-Json
    foreach ($payload in $manifest.payloads) {
        $file = Join-Path $PSScriptRoot $payload.name
        if ((Get-Item -LiteralPath $file).Length -ne $payload.size_bytes -or (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant() -ne $payload.sha256) {
            throw ('Installation file checksum failed: ' + $payload.name)
        }
    }
    Write-Host 'Installing VirtIO drivers, QEMU Guest Agent and SPICE Agent...'
    $bundleLog = Join-Path $logs ($stamp + '-virtio.log')
    $bundle = Start-Process -FilePath (Join-Path $PSScriptRoot 'virtio-win-guest-tools-0.1.285.exe') -ArgumentList ('/install /passive /norestart /log "' + $bundleLog + '"') -Wait -PassThru
    if ($bundle.ExitCode -notin @(0, 3010)) { throw ('VirtIO installer failed: ' + $bundle.ExitCode + '. See ' + $bundleLog) }
    Write-Host 'Installing SPICE WebDAV service...'
    $davLog = Join-Path $logs ($stamp + '-webdav.log')
    $davMsi = Join-Path $PSScriptRoot 'spice-webdavd-x64-2.4.msi'
    $dav = Start-Process -FilePath 'msiexec.exe' -ArgumentList ('/i "' + $davMsi + '" /passive /norestart /L*v "' + $davLog + '"') -Wait -PassThru
    if ($dav.ExitCode -notin @(0, 3010)) { throw ('WebDAV installer failed: ' + $dav.ExitCode + '. See ' + $davLog) }
    foreach ($name in @('QEMU-GA', 'vdservice', 'spice-agent', 'spice-webdavd')) {
        $service = Get-Service -Name $name -ErrorAction SilentlyContinue
        if ($service) {
            Set-Service -Name $name -StartupType Automatic
            try { Start-Service -Name $name -ErrorAction Stop } catch { Write-Warning ('Service not ready yet: ' + $name + '. Restart Windows and check the service.') }
        }
    }
    Write-Host ('Installation completed. Logs: ' + $logs)
    Write-Host 'Restart Windows before using guest tools. Palette detects live service capabilities after boot and login.'
    Write-Host 'Clipboard and QGA file transfer use their existing channels. Windows automatic resolution is not provided by this disc.'
    Stop-Transcript | Out-Null
    exit 0
} catch {
    Write-Host ('Installation failed: ' + $_.Exception.Message) -ForegroundColor Red
    try { Stop-Transcript | Out-Null } catch { }
    exit 1
}
