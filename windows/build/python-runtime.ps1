<#
.SYNOPSIS
    Build the self-contained embedded Python runtime that ships inside Project CHAOS.

.DESCRIPTION
    The installed product must not require Python to be preinstalled.  Nobody is
    going to babysit a Python install on a machine in a container next to a
    battery bank, and "the operator upgraded Python and the control plane stopped
    starting" is not an acceptable failure mode.  So the product carries its own
    interpreter.

    This script turns the official Windows embeddable CPython distribution into
    a complete <install root>\python\ tree:

        download embeddable zip  -> verify SHA-256 -> extract
        rewrite pythonXY._pth    -> enable site and Lib\site-packages
        bootstrap pip            -> from a hash-pinned wheel, no get-pip.py
        pip install              -> requirements.txt, then the homestead_twin package
        copy the web assets      -> they are NOT in the wheel; see WEB ASSETS below
        prune                    -> build-only packages, debug symbols
        precompile               -> .pyc, because Program Files is read-only at runtime
        verify                   -> imports, CLI, offline validate, UI assets present

    INTEGRITY.  Every download is pinned by SHA-256 in python-runtime.lock.json and
    checked before use.  There is no switch to skip the check.  The digests in that
    file were taken from bytes carrying a good OpenPGP signature from the CPython
    Windows release manager; the procedure is written down in the lock file so it
    can be repeated rather than trusted.

    WEB ASSETS.  src/homestead_twin/web/ is not a Python package (no __init__.py)
    and pyproject.toml declares no package-data, so `pip install .` does NOT ship
    it.  But homestead_twin/api/app.py computes WEB_DIR as
    <package dir>/../web and only mounts /ui if that directory exists.  This
    script therefore copies the web tree into site-packages explicitly and then
    asserts WEB_DIR resolves to a real annunciator.html.  Without that step the
    operator UI silently does not exist -- the API answers, /ui 404s, and nothing
    logs an error.

.PARAMETER PythonVersion
    Key from python-runtime.lock.json ("3.11.9" by default).  3.11.9 is the last
    CPython 3.11 with Windows binaries and matches the version the Python test
    suite runs against.

.PARAMETER Destination
    Where to build the tree.  Defaults to windows/build/out/stage/python, i.e.
    straight into the staging tree the installer packages.

.PARAMETER ConstraintsFile
    Optional pip constraints file (ideally with --require-hashes style pins) so a
    shipped runtime can be reproduced byte for byte later.  Generate one with
    `pip freeze` from a known-good tree; see docs/windows-deployment.md.

.PARAMETER IncludePostgres
    Also install the "postgres" extra (psycopg + alembic).  Off by default: the
    Windows product runs on SQLite under %ProgramData%.

.PARAMETER NoPrune
    Keep pip, setuptools and wheel in the shipped tree.  Useful when debugging a
    dependency problem on the property; roughly 15 MB larger.

.PARAMETER Force
    Delete an existing tree at -Destination first.

.EXAMPLE
    .\python-runtime.ps1
    Build the default 3.11.9 runtime into windows\build\out\stage\python.

.NOTES
    Windows only -- it runs python.exe.  Requires outbound HTTPS at BUILD time.
    The installed product never downloads anything.
#>
[CmdletBinding()]
param(
    [string]$PythonVersion,
    [string]$Destination,
    [string]$ConstraintsFile,
    [switch]$IncludePostgres,
    [switch]$NoPrune,
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'Chaos.Build.psm1') -Force

$paths  = Get-ChaosPaths
$layout = Get-ChaosLayout

Assert-ChaosWindows -Because 'Building the embedded Python runtime'

# --- lock file -------------------------------------------------------------

if (-not (Test-Path $paths.Lock)) {
    Stop-Chaos -Message "Missing $($paths.Lock)" -Hint 'The pinned download manifest is part of the build; it is not optional.'
}
$lock = Get-Content -Raw $paths.Lock | ConvertFrom-Json

if (-not $PythonVersion) { $PythonVersion = $lock.default }
$runtimeProp = $lock.runtimes.PSObject.Properties[$PythonVersion]
if (-not $runtimeProp) {
    $known = ($lock.runtimes.PSObject.Properties | ForEach-Object { $_.Name }) -join ', '
    Stop-Chaos -Message "No pin for Python $PythonVersion." `
               -Hint "python-runtime.lock.json knows about: $known. Adding a version means downloading it, verifying its OpenPGP signature and recording the digest -- see the provenance block in that file."
}
$runtime = $runtimeProp.Value

if (-not $Destination) { $Destination = Join-Path $paths.Stage $layout.PythonDirName }

Write-ChaosStep "Embedded Python runtime $($runtime.version)"
Write-ChaosDetail "destination : $Destination"
Write-ChaosDetail "source      : $($runtime.url)"
foreach ($n in $runtime.notes) { if ($n) { Write-ChaosDetail "note        : $n" } }

if (Test-Path $Destination) {
    if ($Force) { Remove-ChaosDirectory -Path $Destination }
    else {
        Stop-Chaos -Message "$Destination already exists." -Hint 'Re-run with -Force to rebuild it, or delete it by hand.'
    }
}

# --- fetch and verify ------------------------------------------------------

Write-ChaosStep 'Fetching pinned artefacts'

$embedZip = Get-ChaosVerifiedFile `
    -Url $runtime.url `
    -Sha256 $runtime.sha256 `
    -ExpectedSize $runtime.size `
    -Destination (Join-Path $paths.Downloads ([IO.Path]::GetFileName($runtime.url)))

$pipWheel = Get-ChaosVerifiedFile `
    -Url $lock.pip.url `
    -Sha256 $lock.pip.sha256 `
    -ExpectedSize $lock.pip.size `
    -Destination (Join-Path $paths.Downloads $lock.pip.filename)

# --- extract ---------------------------------------------------------------

Write-ChaosStep 'Extracting the embeddable distribution'
New-Item -ItemType Directory -Path $Destination -Force | Out-Null
Expand-Archive -LiteralPath $embedZip -DestinationPath $Destination -Force

$pythonExe = Join-Path $Destination 'python.exe'
if (-not (Test-Path $pythonExe)) {
    Stop-Chaos -Message "python.exe is not in the extracted tree at $Destination." `
               -Hint 'The embeddable zip layout changed. Nothing downstream works without <install root>\python\python.exe -- that path is a contract with the supervisor.'
}

# The embeddable distribution ships the stdlib in pythonXY.zip and an isolated
# pythonXY._pth that deliberately keeps site disabled.  pip, entry-point scripts
# and site-packages all need site, so the path file is rewritten rather than
# patched: writing it whole means the shipped configuration is visible in this
# script instead of being the result of a regex against an upstream file.
$pth = Get-ChildItem -Path $Destination -Filter 'python*._pth' | Select-Object -First 1
if (-not $pth) {
    Stop-Chaos -Message "No python*._pth found in $Destination." -Hint 'Expected in every embeddable distribution.'
}
$stdlibZip = "python$($runtime.tag).zip"
if (-not (Test-Path (Join-Path $Destination $stdlibZip))) {
    Stop-Chaos -Message "Expected stdlib archive $stdlibZip is missing." -Hint "The 'tag' field for $($runtime.version) in python-runtime.lock.json does not match the distribution."
}

@(
    $stdlibZip
    '.'
    'Lib\site-packages'
    ''
    '# Written by windows/build/python-runtime.ps1.'
    '# site is enabled deliberately: without it there is no site-packages, so no'
    '# homestead_twin, no uvicorn and no entry-point scripts. The tree stays'
    '# isolated from any other Python on the machine because a ._pth file'
    '# suppresses PYTHONPATH and the registry install path.'
    'import site'
) | Set-Content -LiteralPath $pth.FullName -Encoding ASCII

Write-ChaosDetail "rewrote $($pth.Name)"

New-Item -ItemType Directory -Path (Join-Path $Destination 'Lib\site-packages') -Force | Out-Null

# --- bootstrap pip ---------------------------------------------------------
#
# The embeddable distribution has no pip and no ensurepip.  A wheel is a zip and
# zipimport can import from it, so pip installs itself out of its own wheel.
# One hash-pinned file, no get-pip.py whose contents change without notice.

Write-ChaosStep 'Bootstrapping pip'
$pipInWheel = Join-Path $pipWheel 'pip'
Invoke-ChaosNative -FilePath $pythonExe -What 'pip bootstrap' -Arguments @(
    $pipInWheel, 'install', '--no-index', '--no-cache-dir',
    '--no-warn-script-location', $pipWheel
)

$pipArgsCommon = @('-m', 'pip', 'install', '--no-cache-dir', '--no-warn-script-location', '--disable-pip-version-check')
if ($ConstraintsFile) {
    if (-not (Test-Path $ConstraintsFile)) { Stop-Chaos -Message "Constraints file not found: $ConstraintsFile" }
    $pipArgsCommon += @('--constraint', (Resolve-Path $ConstraintsFile).Path)
    Write-ChaosDetail "constraints : $ConstraintsFile"
}

# setuptools and wheel are installed so the project can be built WITHOUT pip's
# build isolation.  Build isolation spawns a second interpreter, and a second
# interpreter under a ._pth file is exactly where embeddable builds go wrong.
# Both are pruned again below.
Write-ChaosStep 'Installing the build backend'
Invoke-ChaosNative -FilePath $pythonExe -What 'pip install setuptools/wheel' `
    -Arguments ($pipArgsCommon + @('setuptools>=68', 'wheel'))

# --- install the platform --------------------------------------------------

Write-ChaosStep 'Installing platform dependencies'
$requirements = Join-Path $paths.Repo 'requirements.txt'
Invoke-ChaosNative -FilePath $pythonExe -What 'pip install -r requirements.txt' `
    -Arguments ($pipArgsCommon + @('-r', $requirements))

Write-ChaosStep 'Installing the homestead_twin package'
$target = $paths.Repo
if ($IncludePostgres) { $target = "$($paths.Repo)[postgres]" }
Invoke-ChaosNative -FilePath $pythonExe -What 'pip install homestead-twin' `
    -Arguments ($pipArgsCommon + @('--no-build-isolation', $target))

# --- web assets ------------------------------------------------------------

Write-ChaosStep 'Installing the operator UI assets'
$sitePackages = Join-Path $Destination 'Lib\site-packages'
$pkgWeb  = Join-Path (Join-Path $sitePackages 'homestead_twin') 'web'
$srcWeb  = Join-Path (Join-Path (Join-Path $paths.Repo 'src') 'homestead_twin') 'web'

if (-not (Test-Path $srcWeb)) {
    Stop-Chaos -Message "Missing $srcWeb" -Hint 'The operator UI source is part of the repository.'
}
if (Test-Path $pkgWeb) { Remove-Item -LiteralPath $pkgWeb -Recurse -Force }
Copy-Item -LiteralPath $srcWeb -Destination $pkgWeb -Recurse -Force
Write-ChaosDetail "copied web/ into site-packages\homestead_twin\ (it is not in the wheel)"

# --- prune -----------------------------------------------------------------

if (-not $NoPrune) {
    Write-ChaosStep 'Pruning build-only content'

    # Deliberately conservative.  Nothing here is imported at runtime by the
    # platform or by fastapi/uvicorn/sqlalchemy/pydantic/paho/httpx.  Anything
    # less obvious is left in place: 15 MB of dead weight on a homestead server
    # costs nothing, and a missing module at 03:00 during a black start costs a
    # great deal.  The verification block below is what proves the pruning did
    # not take something load-bearing.
    $prunePatterns = @(
        'Lib\site-packages\pip'
        'Lib\site-packages\pip-*.dist-info'
        'Lib\site-packages\setuptools'
        'Lib\site-packages\setuptools-*.dist-info'
        'Lib\site-packages\pkg_resources'
        'Lib\site-packages\_distutils_hack'
        'Lib\site-packages\distutils-precedence.pth'
        'Lib\site-packages\wheel'
        'Lib\site-packages\wheel-*.dist-info'
        'Scripts\pip.exe'
        'Scripts\pip3.exe'
        'Scripts\pip3.*.exe'
        'Scripts\wheel.exe'
    )
    $removed = 0
    foreach ($pattern in $prunePatterns) {
        Get-Item -Path (Join-Path $Destination $pattern) -ErrorAction SilentlyContinue | ForEach-Object {
            Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
            $removed++
        }
    }
    Get-ChildItem -Path $Destination -Recurse -Include '*.pdb' -File -ErrorAction SilentlyContinue |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue; $removed++ }

    Write-ChaosDetail "pruned $removed item(s)"
}
else {
    Write-ChaosWarning 'pruning skipped (-NoPrune): pip and setuptools ship in the product'
}

# --- precompile ------------------------------------------------------------
#
# The shipped tree lives under %ProgramFiles%, which the service account cannot
# write to.  Without .pyc files Python recompiles every module on every start and
# throws the result away, which shows up as a service that takes many seconds to
# come up after a power event.  unchecked-hash invalidation is used because the
# installer does not guarantee source mtimes survive, and an mtime-invalidated
# .pyc in a read-only tree is a .pyc that never gets used.

Write-ChaosStep 'Precompiling to bytecode'
& $pythonExe -m compileall -q -j 0 --invalidation-mode unchecked-hash (Join-Path $Destination 'Lib\site-packages') | Out-Null
if ($LASTEXITCODE -ne 0) {
    # compileall returns non-zero if ANY file fails to compile, including files
    # in third-party packages that are intentionally Python-2 or template files.
    Write-ChaosWarning 'compileall reported errors; some modules will compile at first import instead'
}

# --- verify ----------------------------------------------------------------
#
# Everything above is mechanical.  This is the part that decides whether the
# tree gets packaged.

Write-ChaosStep 'Verifying the runtime'

$appDir     = Join-Path $paths.Stage $layout.AppDirName
$stagedData = Join-Path $appDir 'data'
$stagedSchemas = Join-Path $appDir 'schemas'
$stagedTools   = Join-Path $appDir 'tools'
$haveApp = (Test-Path $stagedData) -and (Test-Path $stagedSchemas) -and (Test-Path $stagedTools)

Invoke-ChaosNative -FilePath $pythonExe -What 'import check' -Arguments @('-c', @'
import importlib, sys
mods = ["homestead_twin", "homestead_twin.cli", "homestead_twin.api.app",
        "simulator", "fastapi", "uvicorn", "sqlalchemy", "pydantic",
        "pydantic_settings", "yaml", "jsonschema", "paho.mqtt.client", "httpx"]
for m in mods:
    importlib.import_module(m)
import homestead_twin
print("homestead_twin", homestead_twin.__version__, "on Python", sys.version.split()[0])
'@)

# The single most valuable check here: WEB_DIR is computed relative to the
# INSTALLED package, so this proves /ui will actually mount at runtime.
Invoke-ChaosNative -FilePath $pythonExe -What 'operator UI asset check' -Arguments @('-c', @'
import sys
from homestead_twin.api.app import WEB_DIR
missing = [n for n in ("index.html", "annunciator.html", "annunciator.js", "app.js", "styles.css")
           if not (WEB_DIR / n).is_file()]
if missing or not WEB_DIR.is_dir():
    sys.exit("operator UI assets missing under %s: %s" % (WEB_DIR, missing))
print("web assets OK:", WEB_DIR)
'@)

$homesteadExe = Join-Path $Destination 'Scripts\homestead-twin.exe'
if (-not (Test-Path $homesteadExe)) {
    Stop-Chaos -Message "Console script missing: $homesteadExe" `
               -Hint 'The CLI is the operator interface on a node with no browser; it must be installed.'
}
Invoke-ChaosNative -FilePath $homesteadExe -What 'homestead-twin --version' -Arguments @('--version')

if ($haveApp) {
    # Offline design-package validation, exactly as it works in the container
    # image today. This is what proves data/, schemas/ and tools/ are laid out
    # so that `validate` and `load-all` need no repository checkout.
    Write-ChaosDetail 'running an offline design-package validation against the staged payload'
    $saved = @{
        Data   = $env:HOMESTEAD_DATA_DIR
        Schema = $env:HOMESTEAD_SCHEMA_DIR
    }
    try {
        $env:HOMESTEAD_DATA_DIR   = $stagedData
        $env:HOMESTEAD_SCHEMA_DIR = $stagedSchemas
        Invoke-ChaosNative -FilePath $homesteadExe -What 'homestead-twin validate' -Arguments @('validate')
    }
    finally {
        $env:HOMESTEAD_DATA_DIR   = $saved.Data
        $env:HOMESTEAD_SCHEMA_DIR = $saved.Schema
    }
}
else {
    Write-ChaosWarning "app payload not staged yet, so 'homestead-twin validate' was not exercised."
    Write-ChaosWarning "Run build.ps1 -Task stage (which stages app\ first, then this script) to check it."
}

$size = (Get-ChildItem -Path $Destination -Recurse -File | Measure-Object -Property Length -Sum).Sum
Write-ChaosStep 'Embedded runtime complete'
Write-ChaosDetail ("path : {0}" -f $Destination)
Write-ChaosDetail ("size : {0:N0} MB" -f ($size / 1MB))
Write-ChaosDetail ("contract: <install root>\{0}" -f $layout.PythonExeRelPath)
