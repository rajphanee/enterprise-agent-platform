# ---------------------------------------------------------------------------
# validate.ps1 - Windows PowerShell version of validate.sh
#
#   powershell -ExecutionPolicy Bypass -File scripts\validate.ps1
# ---------------------------------------------------------------------------
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
$py = if ($env:PYTHON) { $env:PYTHON } else { "python" }
$tmp = Join-Path ([IO.Path]::GetTempPath()) ("eap-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $tmp | Out-Null
$env:AGENT_STATE_DIR = $tmp
if (-not $env:AGENT_MODE) { $env:AGENT_MODE = "offline" }

function Check($name, $expected, [scriptblock]$cmd) {
    & $cmd *> $null
    if ($LASTEXITCODE -ne $expected) { Write-Host "FAIL: $name (exit $LASTEXITCODE)"; Remove-Item -Recurse -Force $tmp; exit 1 }
    Write-Host "PASS: $name"
}

try {
    Check "Python 3.9+"        0 { & $py -c "import sys; assert sys.version_info >= (3, 9)" }
    Check "pytest installed"   0 { & $py -c "import pytest" }
    Check "syntax check"       0 { & $py -m compileall -q agent_platform evals tests }
    Check "unit tests"         0 { & $py -m pytest -q tests }
    Check "evals"              0 { & $py evals/run_evals.py }
    Check "demo"               0 { & $py -m agent_platform demo }
    Check "bad role -> 1"      1 { & $py -m agent_platform ask "hello" --role ceo }
    Check "missing file -> 1"  1 { & $py -m agent_platform vendor --file nope.json }
    $audit = Join-Path $tmp "audit.jsonl"
    (Get-Content $audit) -replace '"alice"', '"mallory"' | Set-Content $audit
    Check "tamper -> 3"        3 { & $py -m agent_platform audit verify }
    Write-Host "`nALL CHECKS PASSED"
} finally {
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}
