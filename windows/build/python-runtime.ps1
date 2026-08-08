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
        write sitecustomize.py   -> shut the per-user site directory out; see ISOLATION
        bootstrap pip            -> from a hash-pinned wheel, no get-pip.py
        pip install              -> requirements.txt, then the chaos package,
                                    WHEELS ONLY; see NATIVE WHEELS below
        copy the web assets      -> they are NOT in the wheel; see WEB ASSETS below
        relocate the launchers   -> Scripts\*.exe must survive the MSI; see RELOCATION
        prune                    -> build-only packages, debug symbols
        precompile               -> .pyc, because Program Files is read-only at runtime
        verify                   -> verify-runtime.py, then the offline validate

    INTEGRITY.  Every download is pinned by SHA-256 in python-runtime.lock.json and
    checked before use.  There is no switch to skip the check.  The digests in that
    file were taken from bytes carrying a good OpenPGP signature from the CPython
    Windows release manager; the procedure is written down in the lock file so it
    can be repeated rather than trusted.

    ISOLATION.  A ._pth file makes CPython ignore PYTHONPATH and the registry, but
    it CANNOT switch off the per-user site directory
    (%APPDATA%\Python\Python311\site-packages).  Left alone, that directory is on
    sys.path -- which means (a) at BUILD time pip sees whatever the person at the
    keyboard once installed with `pip install --user` and skips those packages, so
    the shipped tree is silently incomplete, and (b) at RUN time those same
    packages shadow the ones the product ships.  Verified, not assumed: on a build
    machine with idna and certifi in the user site, pip installed neither into the
    tree and the resulting runtime imported them from the developer's profile.  So
    the script writes a sitecustomize.py before pip runs, and verify-runtime.py
    asserts the user site is gone.

    NATIVE WHEELS.  pydantic-core, greenlet (via SQLAlchemy), rpds-py, PyYAML,
    httptools, watchfiles and websockets are compiled.  Every install below passes
    --only-binary=:all: so pip must use a cp311 win_amd64 wheel and fails loudly if
    one does not exist.  Without it pip falls back to building an sdist, and that
    fails in a way nobody can read: pip's default build isolation hands the build
    backend to the child interpreter through PYTHONPATH, which THIS interpreter
    ignores (that is what a ._pth file does), and its alternative venv-based
    isolation needs `python -m venv`, which the embeddable distribution does not
    ship at all.  A wheels-only policy is the only mode that works here.

    RELOCATION.  pip writes an absolute interpreter path into every console-script
    .exe it creates, taken from the interpreter running pip -- i.e. the STAGING
    path.  The MSI then moves the tree to %ProgramFiles%, and every launcher dies
    with "Fatal error in launcher: Unable to create process using ...".  Nothing
    catches it before a customer does, because in the staging tree the path is
    still valid.  relocate-launchers.py rewrites each shebang to the launcher's
    own documented relative form; see that file for the reference to the C code
    that implements it.

    WEB ASSETS.  src/chaos/web/ is not a Python package (no __init__.py)
    and pyproject.toml declares no package-data, so `pip install .` does NOT ship
    it.  But chaos/api/app.py computes WEB_DIR as
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
#
# What a ._pth file actually does, from CPython 3.11.9 Modules/getpath.py:
#
#     if pth_dir:                      # a ._pth was found next to the executable
#         use_environment = 0          # PYTHONPATH and PYTHONHOME are DISCARDED
#         home = pth_dir
#         pythonpath = []
#     ...
#     if pth:
#         config['isolated'] = 1
#         config['use_environment'] = 0
#         config['site_import'] = 0    # unless a line says exactly "import site"
#         config['safe_path'] = 1
#
# Two consequences worth writing down because both are invisible at run time:
#
#   1. PYTHONPATH IS IGNORED.  Nothing can inject a path into this interpreter
#      through the environment.  That is the behaviour we want -- but it also
#      means the supervisor's development-layout code path
#      (PythonRuntimeResolver.TryEmbedded, which sets PYTHONPATH=<install
#      root>\app\src when that directory exists) would have no effect here.  The
#      staged tree must therefore NOT contain app\src; there is a check for that
#      at the end of this script.
#   2. Env vars that are not about the path still work.  PYTHONUNBUFFERED,
#      PYTHONIOENCODING and PYTHONDONTWRITEBYTECODE -- all three set by
#      BackendSupervisor.BuildStartSpec -- are read before getpath runs and are
#      honoured.  Verified by running a real 3.11 interpreter under a ._pth file,
#      not inferred from the documentation.
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
    '# Lib\site-packages is listed explicitly, so imports work even if site is'
    '# ever disabled. "import site" is here as well because pip, .pth files and'
    '# sitecustomize.py all need site.main() to run. The tree stays isolated'
    '# from any other Python on the machine because a ._pth file suppresses'
    '# PYTHONPATH, PYTHONHOME and the registry install path.'
    'import site'
) | Set-Content -LiteralPath $pth.FullName -Encoding ASCII

Write-ChaosDetail "rewrote $($pth.Name)"

$sitePackages = Join-Path $Destination 'Lib\site-packages'
New-Item -ItemType Directory -Path $sitePackages -Force | Out-Null

# --- shut out the per-user site directory ----------------------------------
#
# This has to happen BEFORE pip runs, not after.  site.main() adds
# %APPDATA%\Python\Python311\site-packages to sys.path and a ._pth file has no
# way to stop it: getpath sets config['isolated'], but the user-site switch is
# read from the command line (-s) or PYTHONNOUSERSITE long before that, so it
# stays on.  With it on, pip treats everything in the developer's own profile as
# already installed and does not put it in the tree.  That is not theoretical:
# on a machine with idna and certifi in the user site, pip installed neither,
# and httpx in the "finished" runtime imported them from the developer's
# %APPDATA%.  That tree would have failed on the first customer machine.
#
# sitecustomize is imported by site.main() from the first sys.path entry that
# has it.  In this tree the search order is pythonXY.zip (no sitecustomize --
# checked), then the runtime directory, then Lib\site-packages, and the user
# site is APPENDED after those, so it cannot shadow this file.
#
# Everything is inside try/except on purpose: a sitecustomize that raises breaks
# every single interpreter start, including pip's, and the failure is reported
# as a bare traceback before anything else runs.  Failing open here is right --
# verify-runtime.py is what proves it actually took effect.
@(
    '# Written by windows/build/python-runtime.ps1. Do not edit in the install tree.'
    '#'
    '# The embedded runtime must import only what the product ships. A ._pth file'
    '# already discards PYTHONPATH and the registry, but it cannot switch off the'
    '# per-user site directory, so that is done here.'
    'try:'
    '    import site as _site'
    '    import sys as _sys'
    '    _user = _site.getusersitepackages()'
    '    if isinstance(_user, str):'
    '        _sys.path[:] = [p for p in _sys.path if p != _user]'
    '    _site.ENABLE_USER_SITE = False'
    'except Exception:'
    '    # Never let this stop the interpreter from starting.'
    '    pass'
) | Set-Content -LiteralPath (Join-Path $sitePackages 'sitecustomize.py') -Encoding ASCII

Write-ChaosDetail 'wrote sitecustomize.py (keeps %APPDATA% site-packages out of the tree)'

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

# WHEELS ONLY.  See NATIVE WHEELS in the header: an sdist cannot be built by this
# interpreter in any reliable way, and a locally compiled binary is a binary
# nobody tested.  Every install of a NAMED requirement goes through this list.
$pipArgsWheelsOnly = $pipArgsCommon + @('--only-binary=:all:')

# Turn pip's "no matching distribution" into the sentence that explains it.
function Install-ChaosWheels {
    param(
        [Parameter(Mandatory)][string]$What,
        [Parameter(Mandatory)][string[]]$Requirements
    )
    try {
        Invoke-ChaosNative -FilePath $pythonExe -What $What -Arguments ($pipArgsWheelsOnly + $Requirements)
    }
    catch {
        Stop-Chaos -Message "$What failed." -Hint @'
If pip said "Could not find a version that satisfies the requirement", read it as
"there is no cp311 win_amd64 WHEEL for that package". --only-binary=:all: is
deliberate and must not be removed: this interpreter cannot build an sdist (pip's
build isolation passes the build backend through PYTHONPATH, which a ._pth
interpreter ignores, and the venv fallback needs a venv module the embeddable
distribution does not ship).

Every dependency of this project was checked and every one publishes a
win_amd64/cp311 wheel, so a failure here means a NEW or UPGRADED dependency does
not. The fix is to pin that dependency to a version that ships a wheel, or to
stop depending on it -- not to allow source builds.
'@
    }
}

# setuptools and wheel are installed so the project can be built WITHOUT pip's
# build isolation.  Build isolation spawns a second interpreter, and a second
# interpreter under a ._pth file is exactly where embeddable builds go wrong.
# Both are pruned again below.
Write-ChaosStep 'Installing the build backend'
Install-ChaosWheels -What 'pip install setuptools/wheel' -Requirements @('setuptools>=68', 'wheel')

# --- install the platform --------------------------------------------------

Write-ChaosStep 'Installing platform dependencies'
$requirements = Join-Path $paths.Repo 'requirements.txt'
Install-ChaosWheels -What 'pip install -r requirements.txt' -Requirements @('-r', $requirements)

# The project is installed in two steps, and the order is the point.
#
#   1. Build a wheel from the checkout with --no-deps --no-build-isolation.  That
#      is the ONLY build in the whole script, it is pure Python, and it needs no
#      compiler.
#   2. Install that wheel with --only-binary=:all:, which is what drags in
#      fastapi, uvicorn[standard], sqlalchemy, pydantic and the rest.
#
# Doing it the obvious way instead -- `pip install <repo>` -- would put the local
# source tree and its whole transitive dependency graph under the same pip run,
# and --only-binary=:all: cannot be used there because the checkout is itself a
# source tree.  Splitting it means the dependency resolution is wheels-only while
# the one thing that legitimately needs building still builds.
Write-ChaosStep 'Building the chaos wheel from the checkout'

$wheelHouse = Join-Path $paths.Out 'wheelhouse'
Remove-ChaosDirectory -Path $wheelHouse
New-Item -ItemType Directory -Path $wheelHouse -Force | Out-Null

# setuptools writes build\ and src\*.egg-info into the CHECKOUT while building a
# wheel, and repo\build is not in .gitignore.  Remember what was there so the
# build does not leave litter in someone's working tree.
$repoBuildDir = Join-Path $paths.Repo 'build'
$repoBuildDirExisted = Test-Path $repoBuildDir

Invoke-ChaosNative -FilePath $pythonExe -What 'pip wheel (chaos)' -Arguments @(
    '-m', 'pip', 'wheel', '--no-cache-dir', '--disable-pip-version-check',
    '--no-deps', '--no-build-isolation',
    '--wheel-dir', $wheelHouse, $paths.Repo
)

$builtWheel = @(Get-ChildItem -LiteralPath $wheelHouse -Filter '*.whl' -File)
if ($builtWheel.Count -ne 1) {
    Stop-Chaos -Message "Expected exactly one wheel in $wheelHouse, found $($builtWheel.Count)." `
               -Hint 'pip wheel built nothing, or more than one thing. Read its output above.'
}
Write-ChaosDetail "built   $($builtWheel[0].Name)"

Write-ChaosStep 'Installing the chaos package and its dependencies'
$target = $builtWheel[0].FullName
if ($IncludePostgres) { $target = "$($builtWheel[0].FullName)[postgres]" }
Install-ChaosWheels -What 'pip install chaos' -Requirements @($target)

if (-not $repoBuildDirExisted -and (Test-Path $repoBuildDir)) {
    Remove-ChaosDirectory -Path $repoBuildDir
    Write-ChaosDetail 'removed the build\ directory setuptools left in the checkout'
}

# --- web assets ------------------------------------------------------------

Write-ChaosStep 'Installing the operator UI assets'
$pkgWeb  = Join-Path (Join-Path $sitePackages 'chaos') 'web'
$srcWeb  = Join-Path (Join-Path (Join-Path $paths.Repo 'src') 'chaos') 'web'

if (-not (Test-Path $srcWeb)) {
    Stop-Chaos -Message "Missing $srcWeb" -Hint 'The operator UI source is part of the repository.'
}
if (Test-Path $pkgWeb) { Remove-Item -LiteralPath $pkgWeb -Recurse -Force }
Copy-Item -LiteralPath $srcWeb -Destination $pkgWeb -Recurse -Force
Write-ChaosDetail "copied web/ into site-packages\chaos\ (it is not in the wheel)"

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
    # -Path is wildcard-parsed, which is what makes the patterns above work -- but
    # it also means a '[' anywhere in the destination would be read as a character
    # class and match nothing, so a tree built under, say, C:\src\chaos [wip]\
    # would silently ship pip and setuptools.  Escape the fixed part and leave
    # the pattern alone.  Concatenated rather than Join-Path'd because Join-Path
    # resolves the drive qualifier and an escaped string is not a path.
    $pruneRoot = [Management.Automation.WildcardPattern]::Escape($Destination.TrimEnd('\'))

    $removed = 0
    foreach ($pattern in $prunePatterns) {
        Get-Item -Path ($pruneRoot + '\' + $pattern) -ErrorAction SilentlyContinue | ForEach-Object {
            Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
            $removed++
        }
    }
    Get-ChildItem -LiteralPath $Destination -Recurse -Filter '*.pdb' -File -ErrorAction SilentlyContinue |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue; $removed++ }

    Write-ChaosDetail "pruned $removed item(s)"
}
else {
    Write-ChaosWarning 'pruning skipped (-NoPrune): pip and setuptools ship in the product'
}

# --- make the console scripts relocatable ----------------------------------
#
# See RELOCATION in the header.  This must run AFTER every pip install and after
# the prune (so only the launchers that actually ship get touched), and BEFORE
# the verification, which checks that no absolute build path is left.

Write-ChaosStep 'Making the console-script launchers relocatable'
$relocator = Join-Path $PSScriptRoot 'relocate-launchers.py'
if (-not (Test-Path $relocator)) {
    Stop-Chaos -Message "Missing $relocator" `
               -Hint 'It ships next to this script. Without it every Scripts\*.exe in the packaged product points at the build machine''s staging directory.'
}
Invoke-ChaosNative -FilePath $pythonExe -What 'relocate-launchers.py' -Arguments @($relocator, $Destination)

# --- precompile ------------------------------------------------------------
#
# The shipped tree lives under %ProgramFiles%, which the service account cannot
# write to.  Without .pyc files Python recompiles every module on every start and
# throws the result away, which shows up as a service that takes many seconds to
# come up after a power event.  unchecked-hash invalidation is used because the
# installer does not guarantee source mtimes survive, and an mtime-invalidated
# .pyc in a read-only tree is a .pyc that never gets used.

Write-ChaosStep 'Precompiling to bytecode'
# compileall reports failures on STDOUT, so no 2>&1 is needed -- and it must not
# be used: with $ErrorActionPreference = 'Stop', PowerShell 7.2 and 7.3 turn a
# redirected native stderr line into a terminating error, which would abort the
# build over a warning.  @() so the result is always enumerable.
$compileLog = @(& $pythonExe -m compileall -q -j 0 --invalidation-mode unchecked-hash $sitePackages)
if ($LASTEXITCODE -ne 0) {
    # compileall returns non-zero if ANY file fails to compile, including files
    # in third-party packages that are intentionally Python-2 or template files.
    # Print what it said: "some modules will compile at first import" is fine for
    # a vendored py2 fixture and not fine for chaos\cli.py, and only the output
    # tells you which one happened.
    Write-ChaosWarning 'compileall reported errors; some modules will compile at first import instead'
    foreach ($line in $compileLog) { Write-ChaosDetail "  $line" }
}

# --- verify ----------------------------------------------------------------
#
# Everything above is mechanical.  This is the part that decides whether the
# tree gets packaged.
#
# The checks live in verify-runtime.py rather than in `python.exe -c '...'` here,
# for two reasons:
#
#   * Windows PowerShell 5.1 does not escape double quotes inside a native
#     command argument.  A multi-line -c payload containing " (which any real
#     import list does) is passed to python.exe with the quoting mangled, and the
#     verification step fails with a SyntaxError that has nothing to do with the
#     runtime.  PowerShell 7.3+ escapes it correctly, so the bug appears and
#     disappears depending on which shell the operator opened -- worse than a
#     bug that always happens.  A file path has no quoting problem on either.
#   * The same checks are what the owner runs by hand afterwards, through
#     verify-runtime.ps1.  One implementation, one set of error messages.

Write-ChaosStep 'Verifying the runtime'

$appDir     = Join-Path $paths.Stage $layout.AppDirName
$stagedData = Join-Path $appDir 'data'
$stagedSchemas = Join-Path $appDir 'schemas'
$stagedTools   = Join-Path $appDir 'tools'
$haveApp = (Test-Path $stagedData) -and (Test-Path $stagedSchemas) -and (Test-Path $stagedTools)

$verifier = Join-Path $PSScriptRoot 'verify-runtime.py'
if (-not (Test-Path $verifier)) {
    Stop-Chaos -Message "Missing $verifier" -Hint 'It ships next to this script and is not optional; nothing gets packaged unverified.'
}
Invoke-ChaosNative -FilePath $pythonExe -What 'verify-runtime.py' -Arguments @($verifier)

# The supervisor resolves <install root>\python\python.exe and, if it also finds
# <install root>\app\src\chaos\cli.py, sets PYTHONPATH=<install root>\app\src
# (PythonRuntimeResolver.TryEmbedded).  A ._pth interpreter DISCARDS PYTHONPATH,
# so in that layout the supervisor would report that it is running the platform
# from app\src while actually running it from Lib\site-packages -- and an
# operator who edits app\src on the property to fix something would see no
# effect at all.  The resolver is tested and correct for a normal interpreter;
# what must not happen is a PACKAGE that puts it in that position.
$stagedSource = Join-Path (Join-Path (Join-Path $appDir 'src') 'chaos') 'cli.py'
if (Test-Path $stagedSource) {
    Stop-Chaos -Message "The staged tree contains $stagedSource." `
               -Hint @'
Do not ship app\src alongside the embedded runtime. PythonRuntimeResolver sets
PYTHONPATH=<install root>\app\src when that file exists, and this interpreter
ignores PYTHONPATH because of its ._pth file -- so the supervisor would describe
a source layout that is not the one actually being imported.

The packaged product imports chaos from python\Lib\site-packages. Stage
app\data, app\schemas, app\tools and app\web only.
'@
}

$chaosExe = Join-Path $Destination 'Scripts\chaos.exe'
if (-not (Test-Path $chaosExe)) {
    Stop-Chaos -Message "Console script missing: $chaosExe" `
               -Hint 'The CLI is the operator interface on a node with no browser; it must be installed.'
}

if ($haveApp) {
    # Offline design-package validation, exactly as it works in the container
    # image today. This is what proves data/, schemas/ and tools/ are laid out
    # so that `validate` and `load-all` need no repository checkout.
    Write-ChaosDetail 'running an offline design-package validation against the staged payload'
    $saved = @{
        Data   = $env:CHAOS_DATA_DIR
        Schema = $env:CHAOS_SCHEMA_DIR
    }
    try {
        $env:CHAOS_DATA_DIR   = $stagedData
        $env:CHAOS_SCHEMA_DIR = $stagedSchemas
        Invoke-ChaosNative -FilePath $chaosExe -What 'chaos validate' -Arguments @('validate')
    }
    finally {
        $env:CHAOS_DATA_DIR   = $saved.Data
        $env:CHAOS_SCHEMA_DIR = $saved.Schema
    }
}
else {
    Write-ChaosWarning "app payload not staged yet, so 'chaos validate' was not exercised."
    Write-ChaosWarning "Run build.ps1 -Task stage (which stages app\ first, then this script) to check it."
}

$files = @(Get-ChildItem -LiteralPath $Destination -Recurse -File)
$size  = ($files | Measure-Object -Property Length -Sum).Sum
Write-ChaosStep 'Embedded runtime complete'
Write-ChaosDetail ("path  : {0}" -f $Destination)
Write-ChaosDetail ("size  : {0:N0} MB in {1:N0} files" -f ($size / 1MB), $files.Count)
Write-ChaosDetail ("contract: <install root>\{0}" -f $layout.PythonExeRelPath)
Write-ChaosDetail 'recheck any time with: .\verify-runtime.ps1'
