<#
.SYNOPSIS
    Check that a built embedded Python runtime actually works.

.DESCRIPTION
    This is the "did it work?" command.  It needs nothing except a produced
    python\ tree -- no .NET build, no MSI, no staged app payload, no network --
    so it can be run on the build machine straight after python-runtime.ps1, or
    on an installed machine against %ProgramFiles%\Project CHAOS\python to find
    out whether the install is intact.

    It answers, in order:

        does python.exe run, and is it 3.11.x
        is the ._pth file there and does it do what we think it does
        is the tree isolated (no PYTHONPATH, no per-user site-packages)
        does `import chaos` work
        do fastapi, sqlalchemy, pydantic, paho.mqtt, yaml, jsonschema import
        do the COMPILED modules load (pydantic-core, greenlet, _ssl, _sqlite3)
        are the operator UI assets where chaos.api.app expects them
        does `python -m chaos.cli --help` exit 0   <- how the supervisor starts it
        will Scripts\*.exe still work after the MSI moves the tree
        does Scripts\chaos.exe run

    Every failure names the likely cause.  The checks themselves live in
    verify-runtime.py, which is run BY THE INTERPRETER UNDER TEST -- that is the
    only way to ask questions about a runtime rather than about this shell.

.PARAMETER Path
    The runtime to check: a directory containing python.exe.  Defaults to
    windows\build\out\stage\python, which is what python-runtime.ps1 produces.

    To check an installed product:
        .\verify-runtime.ps1 -Path "$env:ProgramFiles\Project CHAOS\python"

.EXAMPLE
    .\verify-runtime.ps1

.EXAMPLE
    .\verify-runtime.ps1 -Path "C:\Program Files\Project CHAOS\python"

.NOTES
    Windows only -- it runs python.exe.  Needs no network.
#>
[CmdletBinding()]
param(
    [string]$Path
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'Chaos.Build.psm1') -Force

$paths  = Get-ChaosPaths
$layout = Get-ChaosLayout

Assert-ChaosWindows -Because 'Verifying the embedded Python runtime'

if (-not $Path) { $Path = Join-Path $paths.Stage $layout.PythonDirName }

Write-ChaosStep 'Verifying the embedded Python runtime'
Write-ChaosDetail "runtime : $Path"

if (-not (Test-Path -LiteralPath $Path)) {
    Stop-Chaos -Message "No runtime at $Path." -Hint @'
Nothing has been built here yet. Produce one with:

    .\build.ps1 -Task runtime

or point this at an installed product:

    .\verify-runtime.ps1 -Path "$env:ProgramFiles\Project CHAOS\python"
'@
}

$pythonExe = Join-Path $Path 'python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    $found = @(Get-ChildItem -LiteralPath $Path -Filter '*.exe' -File -ErrorAction SilentlyContinue |
               ForEach-Object { $_.Name })
    Stop-Chaos -Message "$pythonExe does not exist." -Hint @"
<install root>\$($layout.PythonExeRelPath) is a contract with the supervisor:
PythonRuntimeResolver looks for exactly that path and treats a python\ directory
without an interpreter in it as a DAMAGED install -- it will refuse to fall back
to a system Python rather than run the platform against unknown versions.

.exe files actually present in that directory: $(if ($found.Count) { $found -join ', ' } else { '(none)' })
"@
}

$verifier = Join-Path $PSScriptRoot 'verify-runtime.py'
if (-not (Test-Path -LiteralPath $verifier)) {
    Stop-Chaos -Message "Missing $verifier" -Hint 'verify-runtime.py ships next to this script; the checks live there.'
}

# Run the checks WITH THE INTERPRETER UNDER TEST.  Deliberately not `-c`: on
# Windows PowerShell 5.1 a native argument containing both whitespace and double
# quotes is passed through with the quoting mangled, so any non-trivial inline
# program fails for reasons that have nothing to do with the runtime.
Write-ChaosDetail "checks  : $verifier"
Write-Host ''

& $pythonExe $verifier
$code = $LASTEXITCODE

Write-Host ''
if ($code -ne 0) {
    Stop-Chaos -Message "The runtime at $Path failed verification (exit code $code)." -Hint @'
The [FAIL] lines above name the likely cause of each failure. In rough order of
how often each one is the answer:

  * The tree was never rebuilt after a dependency changed.
        .\build.ps1 -Task runtime          (rebuilds from scratch, -Force is implied)

  * Something was installed into the tree by hand afterwards.
        Rebuild rather than repair. pip is pruned out of the shipped tree on
        purpose; a runtime that has been hand-edited is not the one that was
        tested.

  * The launchers were not made relocatable, so the tree only works where it was
    built. Rebuild; python-runtime.ps1 runs relocate-launchers.py for you.

  * An antivirus quarantined a .pyd or one of the vcruntime DLLs. Check the AV
    log for the runtime directory before assuming the build is at fault.

Do not package a tree that fails this.
'@
}

Write-ChaosStep 'Runtime verified'
Write-ChaosDetail "path : $Path"
Write-ChaosDetail 'This runtime can start the platform without any Python installed on the machine.'
