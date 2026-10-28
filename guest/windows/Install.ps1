# Runs inside the Windows guest only. No host execution or Windows setup bypasses.
param([switch]$Uninstall)
$ErrorActionPreference = 'Stop'
$helperInstalled = $false
function Get-SerialDriverFamily([int]$Build, [int]$ProductType) {
    if ($ProductType -eq 1) {
        if ($Build -ge 22000) { return 'w11' }
        return 'w10'
    }
    if ($ProductType -notin @(2, 3) -or $Build -lt 14393) {
        throw 'The Windows version has no supported VirtIO serial driver.'
    }
    if ($Build -ge 26100) { return '2k25' }
    if ($Build -ge 20348) { return '2k22' }
    if ($Build -ge 17763) { return '2k19' }
    return '2k16'
}
try {
    $architecture = $env:PROCESSOR_ARCHITECTURE
    if ($env:PROCESSOR_ARCHITEW6432) { $architecture = $env:PROCESSOR_ARCHITEW6432 }
    if ($architecture -ne 'AMD64') { throw 'This disc supports Windows x64 only. ARM64 and x86 are not supported.' }
    if ([Environment]::OSVersion.Version.Major -lt 10) { throw 'Windows 10 / Server 2016 or newer is required.' }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        $arguments = '-NoLogo -NoProfile -ExecutionPolicy Bypass -File "' + $PSCommandPath + '"'
        if ($Uninstall) { $arguments += ' -Uninstall' }
        $elevated = Start-Process -FilePath 'powershell.exe' -ArgumentList $arguments -Verb RunAs -Wait -PassThru
        exit $elevated.ExitCode
    }
    $logs = Join-Path $env:ProgramData 'Palette\GuestTools\Logs'
    New-Item -ItemType Directory -Force -Path $logs | Out-Null
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    Start-Transcript -Path (Join-Path $logs ($stamp + '-install.log')) | Out-Null
    $agentDirectory = Join-Path ([Environment]::GetFolderPath('ProgramFiles')) 'Palette\GuestTools'
    $agentService = Get-Service -Name 'PaletteGuestTools' -ErrorAction SilentlyContinue
    if ($Uninstall) {
        if ($agentService) {
            Stop-Service -Name 'PaletteGuestTools' -Force -ErrorAction Stop
            & sc.exe delete PaletteGuestTools | Out-Host
            if ($LASTEXITCODE -ne 0) { throw 'Could not remove PaletteGuestTools service.' }
        }
        # The target is a fixed dedicated folder, never supplied by an RPC or file path argument.
        if (Test-Path -LiteralPath $agentDirectory) { Remove-Item -LiteralPath $agentDirectory -Recurse -Force }
        Write-Host 'Palette session helper removed. VirtIO, QGA, SPICE and WebDAV remain installed.'
        Stop-Transcript | Out-Null
        exit 0
    }
    $helperManifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'helper-files.json') -Raw | ConvertFrom-Json
    foreach ($item in $helperManifest.files) {
        if ([IO.Path]::GetFileName($item.name) -ne $item.name) { throw 'Invalid helper source name.' }
        $source = Join-Path $PSScriptRoot $item.name
        if ((Get-Item -LiteralPath $source).Length -ne $item.size_bytes -or (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant() -ne $item.sha256) {
            throw ('Palette helper source checksum failed: ' + $item.name)
        }
    }
    $manifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'payloads.json') -Raw | ConvertFrom-Json
    foreach ($payload in $manifest.payloads) {
        $file = Join-Path $PSScriptRoot $payload.name
        if ((Get-Item -LiteralPath $file).Length -ne $payload.size_bytes -or (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant() -ne $payload.sha256) {
            throw ('Installation file checksum failed: ' + $payload.name)
        }
    }
    $os = Get-CimInstance -ClassName Win32_OperatingSystem
    $family = Get-SerialDriverFamily -Build ([int]$os.BuildNumber) -ProductType ([int]$os.ProductType)
    $serialInf = Join-Path $PSScriptRoot ('drivers\vioserial\' + $family + '\amd64\vioser.inf')
    if (-not (Test-Path -LiteralPath $serialInf)) { throw 'The matching VirtIO serial driver is missing.' }
    Write-Host 'Installing the VirtIO serial driver...'
    & pnputil.exe /add-driver $serialInf /install | Out-Host
    if ($LASTEXITCODE -eq 259) {
        Write-Warning 'No matching serial device was updated; its driver may already be current or the device may be absent.'
    } elseif ($LASTEXITCODE -notin @(0, 3010)) {
        throw ('VirtIO serial driver installation failed: ' + $LASTEXITCODE)
    }
    Write-Host 'Installing Palette Windows user-session tools...'
    if ($agentService) {
        Stop-Service -Name 'PaletteGuestTools' -Force -ErrorAction Stop
        (Get-Service -Name 'PaletteGuestTools').WaitForStatus('Stopped', [TimeSpan]::FromSeconds(15))
    }
    New-Item -ItemType Directory -Force -Path $agentDirectory | Out-Null
    # Program Files inherits administrator/SYSTEM write access and ordinary-user read/execute.
    foreach ($name in @('Build-Agent.ps1', 'Palette.Agent.cs', 'Palette.Native.cs', 'Palette.Operations.cs')) {
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination (Join-Path $agentDirectory $name) -Force
    }
    $agentExecutable = Join-Path $agentDirectory 'Palette.GuestTools.exe'
    & (Join-Path $agentDirectory 'Build-Agent.ps1') -OutputPath $agentExecutable
    if ($agentService) {
        & sc.exe config PaletteGuestTools binPath= ('"' + $agentExecutable + '"') start= auto obj= LocalSystem | Out-Host
        if ($LASTEXITCODE -ne 0) { throw 'Could not update PaletteGuestTools service.' }
    } else {
        New-Service -Name 'PaletteGuestTools' -BinaryPathName ('"' + $agentExecutable + '"') -DisplayName 'Palette Windows Guest Tools' -StartupType Automatic | Out-Null
    }
    & sc.exe failure PaletteGuestTools reset= 86400 actions= restart/3000/restart/10000/restart/30000 | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'Could not configure guest helper recovery.' }
    Start-Service -Name 'PaletteGuestTools' -ErrorAction Stop
    $helperInstalled = $true
    Write-Host 'Installing VirtIO drivers, QEMU Guest Agent and SPICE Agent...'
    $bundleLog = Join-Path $logs ($stamp + '-virtio.log')
    $bundle = Start-Process -FilePath (Join-Path $PSScriptRoot 'virtio-win-guest-tools-0.1.285.exe') -ArgumentList ('/install /passive /norestart /log "' + $bundleLog + '"') -Wait -PassThru
    if ($bundle.ExitCode -notin @(0, 3010)) { throw ('VirtIO installer failed: ' + $bundle.ExitCode + '. See ' + $bundleLog) }
    Write-Host 'Installing SPICE WebDAV service...'
    $davLog = Join-Path $logs ($stamp + '-webdav.log')
    $davMsi = Join-Path $PSScriptRoot 'spice-webdavd-x64-2.4.msi'
    $dav = Start-Process -FilePath 'msiexec.exe' -ArgumentList ('/i "' + $davMsi + '" /passive /norestart /L*v "' + $davLog + '"') -Wait -PassThru
    if ($dav.ExitCode -notin @(0, 3010)) { throw ('WebDAV installer failed: ' + $dav.ExitCode + '. See ' + $davLog) }
    if (-not (Get-Service -Name 'spice-webdavd' -ErrorAction SilentlyContinue)) {
        throw 'WebDAV service is missing after installation.'
    }
    # The upstream proxy can exit during Windows startup. Let the service manager
    # recover instead of leaving sharing stopped until an administrator starts it.
    & sc.exe failure spice-webdavd reset= 86400 actions= restart/3000/restart/10000/restart/30000 | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'Could not configure WebDAV service recovery.' }
    foreach ($name in @('QEMU-GA', 'vdservice', 'spice-agent', 'spice-webdavd')) {
        $service = Get-Service -Name $name -ErrorAction SilentlyContinue
        if ($service) {
            Set-Service -Name $name -StartupType Automatic
            try { Start-Service -Name $name -ErrorAction Stop } catch { Write-Warning ('Service not ready yet: ' + $name + '. Restart Windows and check the service.') }
        }
    }
    Write-Host ('Installation completed. Logs: ' + $logs)
    Write-Host 'Restart Windows before using guest tools. Palette detects live service capabilities after boot and login.'
    Write-Host 'Palette detects file/system/App/UIA tools after a normal user logs in. The transport service never executes these operations as SYSTEM.'
    Write-Host 'QGA binary transfers retain their existing channel. Windows automatic resolution is not provided by this disc.'
    Stop-Transcript | Out-Null
    exit 0
} catch {
    Write-Host ('Installation failed: ' + $_.Exception.Message) -ForegroundColor Red
    if ($helperInstalled) { Write-Host 'The Palette helper was installed. Check the serial driver and Palette status; other components may be unavailable.' }
    try { Stop-Transcript | Out-Null } catch { }
    exit 1
}
