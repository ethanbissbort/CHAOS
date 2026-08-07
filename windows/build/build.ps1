<#
.SYNOPSIS
    One entry point for building, testing, staging, packaging and smoke-testing
    Project CHAOS on Windows.

.DESCRIPTION
    Run this from a normal PowerShell prompt on the machine you develop on.  The
    same entry points are what CI would use, but nothing here assumes a CI
    runner: no environment variables have to be pre-set, prerequisites are named
    when they are missing, and every task can be run on its own.

        .\build.ps1 -Task restore     dotnet restore windows\CHAOS.sln
        .\build.ps1 -Task build       build the whole solution (incl. the WinUI 3 shell)
        .\build.ps1 -Task test        dotnet test across the solution
        .\build.ps1 -Task runtime     build the embedded Python tree
        .\build.ps1 -Task publish     publish Chaos.Host and Chaos.Shell, self-contained
        .\build.ps1 -Task stage       assemble the complete install tree in out\stage
        .\build.ps1 -Task package     build the MSI from out\stage
        .\build.ps1 -Task smoke       run the staged tree and check it actually serves
        .\build.ps1 -Task all         runtime + publish + stage + package
        .\build.ps1 -Task clean       delete windows\build\out

    ORDER MATTERS in one place only: -Task stage lays down app\ (data, schemas,
    tools, web) BEFORE building the Python runtime, because the runtime's final
    verification step runs `chaos validate` against that payload.  The
    stage task handles this for you.

    WHAT NEEDS WHAT

        restore/build/test/publish   .NET 10 SDK, and windows\CHAOS.sln
        runtime                      outbound HTTPS (build time only)
        package                      the wix .NET tool; this script installs a
                                     pinned copy under out\tools automatically
        smoke                        a staged tree, and port 8080 free

.PARAMETER Task
    Which task to run.  Default: all.

.PARAMETER Configuration
    Debug or Release.  Default: Release.

.PARAMETER PythonVersion
    Passed through to python-runtime.ps1.  Default comes from the lock file.

.PARAMETER HostProject / ShellProject
    Explicit project paths, if the automatic discovery under windows\src\ picks
    the wrong one.

.EXAMPLE
    .\build.ps1 -Task all
    .\build.ps1 -Task smoke -Verbose

.NOTES
    Windows only.  Written for Windows PowerShell 5.1 and PowerShell 7.
#>
[CmdletBinding()]
param(
    [ValidateSet('restore', 'build', 'test', 'runtime', 'publish', 'stage', 'package', 'smoke', 'all', 'clean')]
    [string]$Task = 'all',

    [ValidateSet('Debug', 'Release')]
    [string]$Configuration = 'Release',

    [string]$RuntimeIdentifier = 'win-x64',
    [string]$PythonVersion,
    [string]$HostProject,
    [string]$ShellProject,
    [string]$ConstraintsFile,
    [switch]$IncludePostgres,
    [int]$SmokeTimeoutSeconds = 120
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'Chaos.Build.psm1') -Force

$paths   = Get-ChaosPaths
$layout  = Get-ChaosLayout
$version = Get-ChaosVersion

# ---------------------------------------------------------------------------
# Project discovery
#
# windows/src/ belongs to other authors and its exact folder names are not this
# script's business.  Find the two publishable apps by project file name and say
# clearly what was found, so a rename produces a sentence rather than a stack
# trace.
# ---------------------------------------------------------------------------

function Find-ChaosProject {
    param(
        [Parameter(Mandatory)][string]$FileName,
        [string]$Override,
        [switch]$Optional
    )
    if ($Override) {
        if (-not (Test-Path $Override)) { Stop-Chaos -Message "Project not found: $Override" }
        return (Resolve-Path $Override).Path
    }
    $srcRoot = Join-Path $paths.Windows 'src'
    if (-not (Test-Path $srcRoot)) {
        if ($Optional) { return $null }
        Stop-Chaos -Message "windows\src does not exist." -Hint 'The .NET projects are authored by the host, supervisor and shell agents; nothing to publish until they land.'
    }
    $found = @(Get-ChildItem -Path $srcRoot -Recurse -Filter $FileName -File -ErrorAction SilentlyContinue)
    if ($found.Count -eq 0) {
        if ($Optional) { return $null }
        Stop-Chaos -Message "Could not find $FileName under windows\src." `
                   -Hint "Pass an explicit path, e.g. -HostProject windows\src\Something\$FileName."
    }
    if ($found.Count -gt 1) {
        Stop-Chaos -Message "Found $($found.Count) copies of $FileName under windows\src." `
                   -Hint ("Disambiguate with an explicit parameter. Candidates:`n         " + (($found | ForEach-Object { $_.FullName }) -join "`n         "))
    }
    return $found[0].FullName
}

# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

function Invoke-Restore {
    Assert-ChaosDotnet | Out-Null
    $sln = Assert-ChaosSolution
    Write-ChaosStep 'Restoring NuGet packages'
    Invoke-ChaosNative -FilePath 'dotnet' -What 'dotnet restore' -Arguments @('restore', $sln)
}

function Invoke-Build {
    Assert-ChaosDotnet | Out-Null
    $sln = Assert-ChaosSolution
    Write-ChaosStep "Building the solution ($Configuration)"
    Write-ChaosDetail 'This is the only step that compiles the WinUI 3 shell; it cannot be done on Linux.'
    Invoke-ChaosNative -FilePath 'dotnet' -What 'dotnet build' -Arguments @(
        'build', $sln, '-c', $Configuration, '--nologo'
    )
}

function Invoke-Test {
    Assert-ChaosDotnet | Out-Null
    $sln = Assert-ChaosSolution
    Write-ChaosStep "Running .NET tests ($Configuration)"
    if (-not (Test-Path $paths.TestResults)) { New-Item -ItemType Directory -Path $paths.TestResults -Force | Out-Null }
    Invoke-ChaosNative -FilePath 'dotnet' -What 'dotnet test' -Arguments @(
        'test', $sln, '-c', $Configuration, '--nologo',
        '--logger', 'trx', '--results-directory', $paths.TestResults
    )
    Write-ChaosDetail "results: $($paths.TestResults)"
}

function Invoke-Runtime {
    $args = @{ Force = $true }
    if ($PythonVersion)   { $args['PythonVersion']   = $PythonVersion }
    if ($ConstraintsFile) { $args['ConstraintsFile'] = $ConstraintsFile }
    if ($IncludePostgres) { $args['IncludePostgres'] = $true }
    & (Join-Path $PSScriptRoot 'python-runtime.ps1') @args
}

function Invoke-Publish {
    Assert-ChaosWindows -Because 'Publishing the WinUI 3 shell'
    Assert-ChaosDotnet | Out-Null

    $hostProj  = Find-ChaosProject -FileName 'Chaos.Host.csproj'  -Override $HostProject
    $shellProj = Find-ChaosProject -FileName 'Chaos.Shell.csproj' -Override $ShellProject -Optional

    Write-ChaosStep 'Publishing .NET applications'
    Write-ChaosDetail "host  : $hostProj"
    if ($shellProj) { Write-ChaosDetail "shell : $shellProj" } else { Write-ChaosWarning 'Chaos.Shell.csproj not found; the desktop shell will not be packaged' }

    Remove-ChaosDirectory -Path $paths.Publish

    # SELF-CONTAINED, on purpose.
    #
    # The homestead may have no internet and certainly has no patch window.  A
    # framework-dependent build turns "someone uninstalled a .NET runtime" into
    # "the control plane does not start", and makes the installer depend on a
    # runtime download at install time.  The extra ~70 MB is the cheapest
    # insurance in the whole product.
    $common = @(
        '-c', $Configuration,
        '-r', $RuntimeIdentifier,
        '--self-contained', 'true',
        '-p:PublishSingleFile=false',      # the supervisor and the MSI both want a real file layout
        '-p:PublishReadyToRun=false',
        '-p:PublishTrimmed=false',         # never trim a control system: reflection is used by the JSON stack
        '--nologo'
    )

    $hostOut = Join-Path $paths.Publish 'host'
    Invoke-ChaosNative -FilePath 'dotnet' -What 'dotnet publish Chaos.Host' -Arguments (
        @('publish', $hostProj) + $common + @('-o', $hostOut)
    )

    if ($shellProj) {
        $shellOut = Join-Path $paths.Publish 'shell'
        # WindowsAppSDKSelfContained bundles the Windows App SDK runtime into the
        # publish output.  Framework-dependent WinUI 3 requires the WinAppSDK
        # runtime to be installed on the target, which means either a redist
        # bundled in the installer or a download at install time.  Neither is
        # acceptable for a machine that may never see the internet, so the shell
        # carries its own copy.  WindowsPackageType=None makes it an ordinary
        # unpackaged desktop app, which is what an MSI can install and what a
        # Windows Service can sit alongside.
        Invoke-ChaosNative -FilePath 'dotnet' -What 'dotnet publish Chaos.Shell' -Arguments (
            @('publish', $shellProj) + $common + @(
                '-p:WindowsAppSDKSelfContained=true',
                '-p:WindowsPackageType=None',
                '-o', $shellOut
            )
        )
    }
}

function Invoke-Stage {
    Write-ChaosStep 'Assembling the install tree'
    Remove-ChaosDirectory -Path $paths.Stage
    New-Item -ItemType Directory -Path $paths.Stage -Force | Out-Null

    # 1. The Python-side payload, first, so the runtime build can validate against it.
    $appDir = Join-Path $paths.Stage $layout.AppDirName
    foreach ($name in @('data', 'schemas', 'tools')) {
        Copy-ChaosTree -Source (Join-Path $paths.Repo $name) -Destination (Join-Path $appDir $name)
    }
    # A second copy of the web assets, next to the payload rather than buried in
    # site-packages, so the .NET gateway can serve them as static files without
    # reaching into the Python tree.  Both copies come from the same source in
    # the same build, so they cannot drift.
    Copy-ChaosTree `
        -Source (Join-Path (Join-Path (Join-Path $paths.Repo 'src') 'chaos') 'web') `
        -Destination (Join-Path $appDir 'web')

    # __pycache__ from a developer's checkout must not ship.
    Get-ChildItem -Path $appDir -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }

    # 2. The embedded interpreter, into <stage>\python.
    Invoke-Runtime

    # 3. The .NET applications, FLAT in the stage root.  See Chaos.Build.psm1:
    #    AppContext.BaseDirectory must equal the install root so that
    #    <install root>\python\python.exe resolves without guessing.
    $hostOut  = Join-Path $paths.Publish 'host'
    $shellOut = Join-Path $paths.Publish 'shell'
    if (-not (Test-Path $hostOut)) {
        Stop-Chaos -Message 'Nothing published yet.' -Hint 'Run .\build.ps1 -Task publish first, or use -Task all.'
    }
    Copy-ChaosTree -Source $hostOut -Destination $paths.Stage
    if (Test-Path $shellOut) { Copy-ChaosTree -Source $shellOut -Destination $paths.Stage }

    # 4. An operator CLI shim.
    #
    #    chaos resolves its workspace from CHAOS_DATA_DIR's parent,
    #    which is <install root>\app.  That is under Program Files and therefore
    #    read-only, so `backup` with no --output would try to write there.  This
    #    shim sets the environment the CLI needs and sends backups to ProgramData.
    $cmd = @(
        '@echo off'
        'rem  Project CHAOS operator CLI.  Generated by windows\build\build.ps1.'
        'rem  Usage:  chaos.cmd status | validate | load-all | backup | export ...'
        'setlocal'
        'set "CHAOS_HOME=%~dp0"'
        'if not defined CHAOS_DATA set "CHAOS_DATA=%ProgramData%\Project CHAOS"'
        'set "CHAOS_DATA_DIR=%~dp0app\data"'
        'set "CHAOS_SCHEMA_DIR=%~dp0app\schemas"'
        'if not defined CHAOS_DATABASE_URL set "CHAOS_DATABASE_URL=sqlite:///%CHAOS_DATA:\=/%/db/homestead.db"'
        'set "PYTHONUTF8=1"'
        'if /i "%~1"=="backup" ('
        '  shift'
        '  if not exist "%CHAOS_DATA%\backups" mkdir "%CHAOS_DATA%\backups"'
        '  "%~dp0python\Scripts\chaos.exe" backup --output "%CHAOS_DATA%\backups" %1 %2 %3 %4 %5 %6 %7 %8'
        ') else ('
        '  "%~dp0python\Scripts\chaos.exe" %*'
        ')'
        'endlocal & exit /b %ERRORLEVEL%'
    )
    Set-Content -LiteralPath (Join-Path $paths.Stage 'chaos.cmd') -Value $cmd -Encoding ASCII
    Write-ChaosDetail 'wrote chaos.cmd (operator CLI shim)'

    # 5. Prove the contract holds before anything is packaged.
    $mustExist = @(
        $layout.HostExe
        $layout.PythonExeRelPath
        'python\Scripts\chaos.exe'
        'python\Lib\site-packages\chaos\web\annunciator.html'
        'app\data\alarm_definitions.yaml'
        'app\schemas\alarm_definitions.schema.json'
        'app\tools\validate_bundle.py'
        'app\web\annunciator.html'
        'chaos.cmd'
    )
    $missing = @()
    foreach ($rel in $mustExist) {
        if (-not (Test-Path (Join-Path $paths.Stage $rel))) { $missing += $rel }
    }
    if ($missing.Count -gt 0) {
        Stop-Chaos -Message "The staged tree is missing $($missing.Count) required path(s)." `
                   -Hint ("Missing:`n         " + ($missing -join "`n         "))
    }

    $size = (Get-ChildItem -Path $paths.Stage -Recurse -File | Measure-Object -Property Length -Sum).Sum
    Write-ChaosStep 'Install tree staged'
    Write-ChaosDetail ("root : {0}" -f $paths.Stage)
    Write-ChaosDetail ("size : {0:N0} MB" -f ($size / 1MB))
}

function Get-WixCommand {
    <#
        Install a pinned wix into out\tools rather than globally: a build script
        that mutates the developer's machine-wide tool set is a build script that
        breaks a different project later.
    #>
    $wixVersion = '5.0.2'
    $wixExe = Join-Path $paths.Tools 'wix.exe'
    if (-not (Test-Path $wixExe)) {
        Write-ChaosStep "Installing the WiX toolset ($wixVersion) into out\tools"
        Invoke-ChaosNative -FilePath 'dotnet' -What 'dotnet tool install wix' -Arguments @(
            'tool', 'install', 'wix', '--version', $wixVersion, '--tool-path', $paths.Tools
        )
    }
    foreach ($ext in @('WixToolset.Util.wixext', 'WixToolset.Firewall.wixext', 'WixToolset.UI.wixext')) {
        Invoke-ChaosNative -FilePath $wixExe -What "wix extension add $ext" -Arguments @(
            'extension', 'add', "$ext/$wixVersion"
        )
    }
    return $wixExe
}

function Invoke-Package {
    Assert-ChaosWindows -Because 'Building an MSI'
    Assert-ChaosDotnet | Out-Null

    if (-not (Test-Path (Join-Path $paths.Stage $layout.HostExe))) {
        Stop-Chaos -Message 'Nothing staged to package.' -Hint 'Run .\build.ps1 -Task stage first, or use -Task all.'
    }

    $wix = Get-WixCommand
    if (-not (Test-Path $paths.Package)) { New-Item -ItemType Directory -Path $paths.Package -Force | Out-Null }
    $msi = Join-Path $paths.Package "ProjectCHAOS-$version-x64.msi"

    Write-ChaosStep "Building $([IO.Path]::GetFileName($msi))"
    $sources = @(Get-ChildItem -Path $paths.Installer -Filter '*.wxs' -File | ForEach-Object { $_.FullName })
    if ($sources.Count -eq 0) { Stop-Chaos -Message "No .wxs files in $($paths.Installer)." }

    Invoke-ChaosNative -FilePath $wix -What 'wix build' -Arguments (
        @('build') + $sources + @(
            '-arch', 'x64',
            '-ext', 'WixToolset.Util.wixext',
            '-ext', 'WixToolset.Firewall.wixext',
            '-ext', 'WixToolset.UI.wixext',
            '-bindpath', "Stage=$($paths.Stage)",
            '-bindpath', "Installer=$($paths.Installer)",
            '-d', "ChaosVersion=$version",
            '-d', "StageDir=$($paths.Stage)",
            '-o', $msi
        )
    )

    Write-ChaosStep 'Installer built'
    Write-ChaosDetail ("msi  : {0}" -f $msi)
    Write-ChaosDetail ("size : {0:N0} MB" -f ((Get-Item $msi).Length / 1MB))
    Write-ChaosDetail 'UNSIGNED. Sign it before it leaves this machine: signtool sign /fd SHA256 /tr <timestamp url> /td SHA256 ...'
}

function Invoke-Smoke {
    <#
        The check that actually matters: does the Python child come up under the
        .NET host, and does the operator UI serve real content?

        This runs the STAGED tree, not an installed one, so it needs no
        administrator rights and leaves nothing behind.  The installed-service
        equivalent is written out step by step in docs/windows-deployment.md.
    #>
    Assert-ChaosWindows -Because 'The smoke test'

    $hostExe = Join-Path $paths.Stage $layout.HostExe
    if (-not (Test-Path $hostExe)) {
        Stop-Chaos -Message "Nothing staged: $hostExe not found." -Hint 'Run .\build.ps1 -Task stage first.'
    }

    $scratch = Join-Path $paths.Out 'smoke'
    Remove-ChaosDirectory -Path $scratch
    foreach ($sub in @('config', 'db', 'logs', 'backups')) {
        New-Item -ItemType Directory -Path (Join-Path $scratch $sub) -Force | Out-Null
    }

    $dbPath = (Join-Path (Join-Path $scratch 'db') 'homestead.db')
    $dbUrl  = 'sqlite:///' + ($dbPath -replace '\\', '/')

    Write-ChaosStep 'Preparing a scratch database (offline, from the staged payload)'
    $chaosCmd = Join-Path $paths.Stage 'chaos.cmd'
    $saved = @{
        Data = $env:CHAOS_DATA; Db = $env:CHAOS_DATABASE_URL
        ApiHost = $env:CHAOS_API_HOST; ApiPort = $env:CHAOS_API_PORT
        Mqtt = $env:CHAOS_MQTT_ENABLED; Home = $env:CHAOS_HOME; Web = $env:CHAOS_WEB_ROOT
    }
    try {
        $env:CHAOS_DATA              = $scratch
        $env:CHAOS_HOME              = $paths.Stage
        $env:CHAOS_WEB_ROOT          = (Join-Path (Join-Path $paths.Stage $layout.AppDirName) 'web')
        $env:CHAOS_DATABASE_URL  = $dbUrl
        $env:CHAOS_API_HOST      = '127.0.0.1'
        $env:CHAOS_API_PORT      = "$($layout.LoopbackPort)"
        $env:CHAOS_MQTT_ENABLED  = 'false'

        Invoke-ChaosNative -FilePath $chaosCmd -What 'chaos.cmd validate' -Arguments @('validate')
        Invoke-ChaosNative -FilePath $chaosCmd -What 'chaos.cmd init-db'  -Arguments @('init-db')
        Invoke-ChaosNative -FilePath $chaosCmd -What 'chaos.cmd load-all' -Arguments @('load-all')

        Write-ChaosStep "Starting $($layout.HostExe) in console mode"
        $log = Join-Path $scratch 'logs\host-console.log'
        $err = Join-Path $scratch 'logs\host-console.err.log'
        $proc = Start-Process -FilePath $hostExe -WorkingDirectory $paths.Stage `
                              -RedirectStandardOutput $log -RedirectStandardError $err `
                              -PassThru -NoNewWindow

        try {
            $healthUrl = "http://127.0.0.1:$($layout.LanPort)/health"
            Write-ChaosDetail "polling $healthUrl (up to $SmokeTimeoutSeconds s)"
            $deadline = (Get-Date).AddSeconds($SmokeTimeoutSeconds)
            $health = $null
            while ((Get-Date) -lt $deadline) {
                if ($proc.HasExited) {
                    Write-Host (Get-Content -Raw -ErrorAction SilentlyContinue $log)
                    Write-Host (Get-Content -Raw -ErrorAction SilentlyContinue $err)
                    Stop-Chaos -Message "$($layout.HostExe) exited with code $($proc.ExitCode) before /health answered."
                }
                try {
                    $r = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 5
                    if ($r.StatusCode -eq 200) { $health = $r.Content; break }
                } catch { Start-Sleep -Seconds 2 }
            }
            if (-not $health) {
                Write-Host (Get-Content -Raw -ErrorAction SilentlyContinue $log)
                Write-Host (Get-Content -Raw -ErrorAction SilentlyContinue $err)
                Stop-Chaos -Message "/health did not answer within $SmokeTimeoutSeconds s." `
                           -Hint "Check $log and $err. Common causes: port $($layout.LanPort) already in use, or the supervisor could not start python\python.exe."
            }
            Write-ChaosDetail "health : $health"

            $uiUrl = "http://127.0.0.1:$($layout.LanPort)/ui/annunciator.html"
            $ui = Invoke-WebRequest -Uri $uiUrl -UseBasicParsing -TimeoutSec 15
            if ($ui.Content -notmatch 'Annunciator Panel') {
                Stop-Chaos -Message "$uiUrl did not return the annunciator page." `
                           -Hint 'Expected the title "Annunciator Panel". A 200 with the wrong body usually means the gateway is serving a fallback page rather than the real assets.'
            }
            Write-ChaosDetail "ui     : $($ui.Content.Length) bytes from /ui/annunciator.html"

            $apiUrl = "http://127.0.0.1:$($layout.LanPort)/api/v1/annunciator"
            $api = Invoke-RestMethod -Uri $apiUrl -TimeoutSec 30
            if (-not $api.bays -or -not $api.summary) {
                Stop-Chaos -Message "$apiUrl returned no bays/summary." -Hint 'load-all did not populate alarm definitions, or the gateway is not proxying /api/v1 to the Python backend.'
            }
            $tiles = ($api.bays | ForEach-Object { $_.tiles.Count } | Measure-Object -Sum).Sum
            if ($tiles -lt 1) {
                Stop-Chaos -Message 'The annunciator returned zero tiles.' -Hint 'The panel is one lamp per alarm definition; zero means the registry is empty.'
            }
            Write-ChaosDetail "api    : $($api.bays.Count) bay(s), $tiles tile(s), summary.total = $($api.summary.total)"

            Write-ChaosStep 'Smoke test passed'
            Write-ChaosDetail 'The Python backend started under the .NET host and served real content.'
        }
        finally {
            if (-not $proc.HasExited) {
                Write-ChaosDetail 'stopping the host'
                # CloseMainWindow does nothing for a console app started with
                # -NoNewWindow, so stop the process tree directly.
                Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
                $proc.WaitForExit(15000) | Out-Null
            }
            Get-Process -Name 'python' -ErrorAction SilentlyContinue |
                Where-Object { $_.Path -and $_.Path.StartsWith($paths.Stage, [StringComparison]::OrdinalIgnoreCase) } |
                ForEach-Object {
                    Write-ChaosWarning "orphaned python child $($_.Id) left behind; stopping it"
                    Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
                }
        }
    }
    finally {
        $env:CHAOS_DATA             = $saved.Data
        $env:CHAOS_HOME             = $saved.Home
        $env:CHAOS_WEB_ROOT         = $saved.Web
        $env:CHAOS_DATABASE_URL = $saved.Db
        $env:CHAOS_API_HOST     = $saved.ApiHost
        $env:CHAOS_API_PORT     = $saved.ApiPort
        $env:CHAOS_MQTT_ENABLED = $saved.Mqtt
    }
}

# ---------------------------------------------------------------------------

Write-ChaosStep "Project CHAOS $version -- task '$Task'"
Write-ChaosDetail "repo : $($paths.Repo)"
Write-ChaosDetail "out  : $($paths.Out)"

switch ($Task) {
    'restore' { Invoke-Restore }
    'build'   { Invoke-Build }
    'test'    { Invoke-Test }
    'runtime' { Invoke-Runtime }
    'publish' { Invoke-Publish }
    'stage'   { Invoke-Stage }
    'package' { Invoke-Package }
    'smoke'   { Invoke-Smoke }
    'clean'   { Remove-ChaosDirectory -Path $paths.Out; Write-ChaosDetail 'clean' }
    'all'     {
        Invoke-Publish
        Invoke-Stage
        Invoke-Package
        Write-ChaosStep 'Done'
        Write-ChaosDetail 'Next: .\build.ps1 -Task smoke   (runs the staged tree and checks it serves)'
    }
}
