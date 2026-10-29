# Compile inside Windows using only its inbox .NET Framework compiler and assemblies.
param([Parameter(Mandatory=$true)][string]$OutputPath)
$ErrorActionPreference = 'Stop'
$framework = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319'
$compiler = Join-Path $framework 'csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) { throw 'The Windows x64 .NET Framework compiler is unavailable.' }
$references = @('System.Web.Extensions.dll', 'System.ServiceProcess.dll')
foreach ($assembly in @('UIAutomationClient.dll', 'UIAutomationTypes.dll', 'WindowsBase.dll')) {
    $references += Join-Path $framework ('WPF\' + $assembly)
}
$arguments = @('/nologo', '/target:exe', '/platform:x64', '/optimize+', '/codepage:65001', ('/out:' + $OutputPath))
foreach ($reference in $references) { $arguments += '/reference:' + $reference }
foreach ($name in @('Palette.Agent.cs', 'Palette.Native.cs', 'Palette.Operations.cs')) {
    $arguments += Join-Path $PSScriptRoot $name
}
& $compiler @arguments
if ($LASTEXITCODE -ne 0) { throw ('Palette guest helper compilation failed: ' + $LASTEXITCODE) }
