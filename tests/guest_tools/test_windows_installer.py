"""Check the Windows tools installer's driver selection without installing anything."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell required")
INSTALLER = Path(__file__).resolve().parents[2] / "guest/windows/Install.ps1"


def test_serial_driver_matches_windows_product_and_build() -> None:
    script = r"""
    $tokens = $null; $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile(
        $env:PALETTE_INSTALL_SCRIPT, [ref]$tokens, [ref]$errors)
    if ($errors.Count) { throw ($errors | Out-String) }
    $function = $ast.Find({ param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Get-SerialDriverFamily'
    }, $true)
    if (-not $function) { throw 'serial driver selection function is missing' }
    Invoke-Expression $function.Extent.Text
    $result = @(
        Get-SerialDriverFamily 19045 1
        Get-SerialDriverFamily 22000 1
        Get-SerialDriverFamily 26100 1
        Get-SerialDriverFamily 14393 3
        Get-SerialDriverFamily 17763 2
        Get-SerialDriverFamily 20348 3
        Get-SerialDriverFamily 26100 3
    )
    foreach ($item in @(@(14392, 3), @(22000, 0))) {
        try { Get-SerialDriverFamily $item[0] $item[1]; throw 'unexpectedly accepted' }
        catch { if ($_.Exception.Message -eq 'unexpectedly accepted') { throw } }
    }
    ConvertTo-Json -Compress -InputObject $result
    """
    env = {**os.environ, "PALETTE_INSTALL_SCRIPT": str(INSTALLER)}
    powershell = shutil.which("powershell.exe")
    assert powershell is not None
    completed = subprocess.run(  # noqa: S603 - fixed PowerShell and repository script
        [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        env=env,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    assert json.loads(completed.stdout) == ["w10", "w11", "w11", "2k16", "2k19", "2k22", "2k25"]
