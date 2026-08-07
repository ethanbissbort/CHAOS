<#
.SYNOPSIS
    Shared helpers for the Project CHAOS Windows build scripts.

.DESCRIPTION
    Everything in windows/build/ is meant to be run by a human at a normal
    PowerShell prompt on the machine that will ship the product, not only by a
    CI runner.  That drives three rules this module enforces:

      1. Prerequisites are checked up front and named precisely.  "dotnet is not
         on PATH" is a useful error; a NullReferenceException four minutes into a
         publish is not.
      2. Nothing is downloaded without a pinned SHA-256.  See
         python-runtime.lock.json for why.
      3. Every path the installer depends on is defined here once, so the build
         scripts, the installer authoring and the documentation cannot drift.

    Written against Windows PowerShell 5.1 (the shell you get by pressing Start
    and typing "powershell") as well as PowerShell 7.  No 6+-only syntax:
    no ternaries, no ??, no -AdditionalChildPath.
#>

Set-StrictMode -Version Latest

# ---------------------------------------------------------------------------
# The install layout.  THIS IS A CONTRACT, not an implementation detail.
#
#   <install root>\                       %ProgramFiles%\Project CHAOS
#     Chaos.Host.exe                      gateway + Windows Service host
#     Chaos.Shell.exe                     WinUI 3 desktop shell and tray icon
#     python\python.exe                   <-- the supervisor resolves THIS
#     app\data|schemas|tools|web          Python-side payload
#
#   <data root>\                          %ProgramData%\Project CHAOS
#     config\ db\ logs\ backups\          operator data; NEVER removed
#
# The .NET files are deliberately flat in the install root so that
# AppContext.BaseDirectory == install root == the parent of python\.  Any layout
# that nests the host in a subfolder makes "python\python.exe relative to the
# install root" ambiguous, and an ambiguous path in a supervisor is a service
# that does not start on the one night it matters.
# ---------------------------------------------------------------------------

$script:ChaosLayout = [ordered]@{
    ProductName      = 'Project CHAOS'
    ServiceName      = 'ChaosHost'
    HostExe          = 'Chaos.Host.exe'
    ShellExe         = 'Chaos.Shell.exe'
    PythonDirName    = 'python'          # <install root>\python
    PythonExeRelPath = 'python\python.exe'
    AppDirName       = 'app'             # <install root>\app
    LanPort          = 8080              # the .NET gateway's LAN listener
    LoopbackPort     = 8081              # default Python child port (loopback only)
}

function Get-ChaosLayout {
    <#  The single source of truth for names, ports and relative paths.  #>
    [CmdletBinding()]
    param()
    return $script:ChaosLayout
}

function Get-ChaosPaths {
    <#
    .SYNOPSIS
        Resolve every directory the build uses, from the location of this module.
    .DESCRIPTION
        windows/build/Chaos.Build.psm1  ->  repo root is two levels up.
        All build output lives under windows/build/out/, which .gitignore already
        excludes.
    #>
    [CmdletBinding()]
    param()

    $buildDir = Split-Path -Parent $PSCommandPath
    $windows  = Split-Path -Parent $buildDir
    $repo     = Split-Path -Parent $windows
    $out      = Join-Path $buildDir 'out'

    return [ordered]@{
        Repo        = $repo
        Windows     = $windows
        Build       = $buildDir
        Installer   = Join-Path $windows 'installer'
        Solution    = Join-Path $windows 'CHAOS.sln'
        Out         = $out
        Downloads   = Join-Path $out 'downloads'
        Publish     = Join-Path $out 'publish'      # per-project publish output
        Stage       = Join-Path $out 'stage'        # mirrors the install root exactly
        Package     = Join-Path $out 'package'      # the .msi
        Tools       = Join-Path $out 'tools'        # locally pinned wix, etc.
        TestResults = Join-Path $out 'testresults'
        Lock        = Join-Path $buildDir 'python-runtime.lock.json'
    }
}

# ---------------------------------------------------------------------------
# Output.  Plain, greppable, and legible in a 80-column console over a slow link
# from a phone in a barn.
# ---------------------------------------------------------------------------

function Write-ChaosStep {
    param([Parameter(Mandatory)][string]$Message)
    Write-Host ''
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-ChaosDetail {
    param([Parameter(Mandatory)][string]$Message)
    Write-Host "    $Message"
}

function Write-ChaosWarning {
    param([Parameter(Mandatory)][string]$Message)
    Write-Host "    WARNING: $Message" -ForegroundColor Yellow
}

function Stop-Chaos {
    <#  Fail with an actionable message.  Hint is printed as a second line.  #>
    param(
        [Parameter(Mandatory)][string]$Message,
        [string]$Hint
    )
    Write-Host ''
    Write-Host "ERROR: $Message" -ForegroundColor Red
    if ($Hint) { Write-Host "       $Hint" -ForegroundColor Red }
    Write-Host ''
    throw $Message
}

function Invoke-ChaosNative {
    <#
    .SYNOPSIS
        Run a native command and fail the build if it returns non-zero.
    .DESCRIPTION
        PowerShell does not stop on a non-zero native exit code, which is how
        build scripts end up cheerfully packaging a failed compile.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [string[]]$Arguments = @(),
        [string]$WorkingDirectory,
        [string]$What
    )

    if (-not $What) { $What = (Split-Path -Leaf $FilePath) }
    Write-ChaosDetail ("$ " + $FilePath + ' ' + ($Arguments -join ' '))

    # Seeded, because Set-StrictMode turns "the command threw before it could set
    # an exit code" into "the variable $code cannot be retrieved" -- which hides
    # the real error behind a StrictMode complaint.
    $code = -1

    $pushed = $false
    if ($WorkingDirectory) { Push-Location $WorkingDirectory; $pushed = $true }
    try {
        & $FilePath @Arguments
        $code = $LASTEXITCODE
    }
    finally {
        if ($pushed) { Pop-Location }
    }

    if ($code -ne 0) {
        Stop-Chaos -Message "$What failed with exit code $code." `
                   -Hint 'The command line above is the one to re-run by hand to see the full output.'
    }
}

# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------

function Assert-ChaosWindows {
    param([string]$Because = 'this task')
    $onWindows = $true
    if (Get-Variable -Name IsWindows -Scope Global -ErrorAction SilentlyContinue) {
        $onWindows = $IsWindows
    }
    if (-not $onWindows) {
        Stop-Chaos -Message "$Because can only run on Windows." `
                   -Hint 'Publishing WinUI 3 / net10.0-windows and building an MSI both require the Windows SDK and Windows Installer.'
    }
}

function Assert-ChaosDotnet {
    <#  Confirm the SDK named in windows/global.json is actually installed.  #>
    [CmdletBinding()]
    param()

    $dotnet = Get-Command dotnet -ErrorAction SilentlyContinue
    if (-not $dotnet) {
        Stop-Chaos -Message 'The .NET SDK is not on PATH.' `
                   -Hint 'Install the .NET 10 SDK from https://dotnet.microsoft.com/download, or open the "Developer PowerShell for VS 2026" prompt, which puts it on PATH.'
    }

    $paths  = Get-ChaosPaths
    $gjPath = Join-Path $paths.Windows 'global.json'
    if (Test-Path $gjPath) {
        $wanted = (Get-Content -Raw $gjPath | ConvertFrom-Json).sdk.version
        $have   = (& dotnet --list-sdks) | ForEach-Object { ($_ -split ' ')[0] }
        $major  = ($wanted -split '\.')[0]
        $match  = @($have | Where-Object { $_ -like "$major.*" })
        if ($match.Count -eq 0) {
            Stop-Chaos -Message "windows/global.json pins SDK $wanted but no $major.x SDK is installed." `
                       -Hint ("Installed SDKs: " + ($have -join ', ') + ". Install the .NET $major SDK, then re-run.")
        }
        Write-ChaosDetail "dotnet SDK: pinned $wanted, using $($match[-1])"
    }
    return $dotnet.Source
}

function Assert-ChaosSolution {
    <#
        windows/CHAOS.sln is created by the lead at integration.  Say so plainly
        rather than letting MSBuild emit "project file does not exist".
    #>
    [CmdletBinding()]
    param()
    $paths = Get-ChaosPaths
    if (-not (Test-Path $paths.Solution)) {
        Stop-Chaos -Message "windows/CHAOS.sln does not exist yet." `
                   -Hint 'The solution is created at integration and must reference Chaos.Host, Chaos.Host.Supervisor and Chaos.Shell. Until it lands, the .NET tasks (build/test/publish) cannot run; the runtime task can.'
    }
    return $paths.Solution
}

function Test-ChaosElevated {
    [CmdletBinding()]
    param()
    try {
        $id = [Security.Principal.WindowsIdentity]::GetCurrent()
        $pr = New-Object Security.Principal.WindowsPrincipal($id)
        return $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    }
    catch { return $false }
}

# ---------------------------------------------------------------------------
# Verified downloads
# ---------------------------------------------------------------------------

function Get-ChaosVerifiedFile {
    <#
    .SYNOPSIS
        Download a file and refuse to return it unless its SHA-256 matches.
    .DESCRIPTION
        Cached in windows/build/out/downloads.  A cached file whose digest no
        longer matches is deleted and re-fetched once; a second mismatch is a
        hard failure.  There is deliberately no switch to skip the check: the
        artefacts this fetches become the interpreter that runs the control
        path, and an installer that quietly accepts whatever the network handed
        it is not something to point at a battery.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Url,
        [Parameter(Mandatory)][string]$Sha256,
        [Parameter(Mandatory)][string]$Destination,
        [long]$ExpectedSize = 0
    )

    $dir = Split-Path -Parent $Destination
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }

    $expected = $Sha256.Trim().ToLowerInvariant()

    for ($attempt = 1; $attempt -le 2; $attempt++) {
        if (-not (Test-Path $Destination)) {
            Write-ChaosDetail "downloading $Url"
            # TLS 1.2 is not the default in Windows PowerShell 5.1 on older builds.
            try {
                [Net.ServicePointManager]::SecurityProtocol =
                    [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
            } catch { }
            $progress = $ProgressPreference
            $ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is ~10x slower with it on
            try {
                Invoke-WebRequest -Uri $Url -OutFile $Destination -UseBasicParsing
            }
            catch {
                Stop-Chaos -Message "Could not download $Url" `
                           -Hint 'This machine needs outbound HTTPS for the BUILD only. The installed product never downloads anything; see docs/windows-deployment.md.'
            }
            finally { $ProgressPreference = $progress }
        }
        else {
            Write-ChaosDetail "cached  $([IO.Path]::GetFileName($Destination))"
        }

        $actual = (Get-FileHash -Path $Destination -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -eq $expected) {
            if ($ExpectedSize -gt 0) {
                $size = (Get-Item $Destination).Length
                if ($size -ne $ExpectedSize) {
                    Stop-Chaos -Message "Size mismatch for $Destination (expected $ExpectedSize bytes, got $size)." `
                               -Hint 'The digest matched but the size did not, which should be impossible. Treat the lock file as suspect.'
                }
            }
            Write-ChaosDetail "sha256  OK  $expected"
            return $Destination
        }

        Write-ChaosWarning "sha256 mismatch for $([IO.Path]::GetFileName($Destination))"
        Write-ChaosWarning "  expected $expected"
        Write-ChaosWarning "  actual   $actual"
        Remove-Item -Force $Destination -ErrorAction SilentlyContinue
    }

    Stop-Chaos -Message "SHA-256 verification failed twice for $Url" `
               -Hint 'Do not work around this. Either the pin in windows/build/python-runtime.lock.json is stale (update it following the provenance procedure in that file) or the download was tampered with. Nothing gets packaged until this matches.'
}

# ---------------------------------------------------------------------------
# Tree assembly
# ---------------------------------------------------------------------------

function Copy-ChaosTree {
    <#
    .SYNOPSIS
        Copy a directory tree into the staging tree, failing on real collisions.
    .DESCRIPTION
        Two self-contained .NET apps published into one folder share most of
        their runtime files.  Identical files are fine and expected; a file that
        exists in both with DIFFERENT content means the two apps disagree about
        a dependency version, and silently letting the last writer win is how a
        shell that works on the bench crashes on the property.  So collisions
        are compared by hash and a genuine conflict stops the build.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Source,
        [Parameter(Mandatory)][string]$Destination,
        [string[]]$ExcludeFile = @()
    )

    if (-not (Test-Path $Source)) {
        Stop-Chaos -Message "Nothing to copy: $Source does not exist."
    }
    if (-not (Test-Path $Destination)) {
        New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    }

    $srcRoot = (Resolve-Path $Source).Path.TrimEnd('\', '/')
    $dstRoot = (Resolve-Path $Destination).Path.TrimEnd('\', '/')
    $conflicts = New-Object System.Collections.Generic.List[string]
    $copied = 0

    Get-ChildItem -Path $srcRoot -Recurse -File | ForEach-Object {
        $rel = $_.FullName.Substring($srcRoot.Length).TrimStart('\', '/')
        if ($ExcludeFile -contains $_.Name) { return }

        $target = Join-Path $dstRoot $rel
        $targetDir = Split-Path -Parent $target
        if (-not (Test-Path $targetDir)) { New-Item -ItemType Directory -Path $targetDir -Force | Out-Null }

        if (Test-Path $target) {
            $a = (Get-FileHash -Path $_.FullName -Algorithm SHA256).Hash
            $b = (Get-FileHash -Path $target      -Algorithm SHA256).Hash
            if ($a -ne $b) { $conflicts.Add($rel) | Out-Null; return }
            return
        }

        Copy-Item -LiteralPath $_.FullName -Destination $target -Force
        $copied++
    }

    if ($conflicts.Count -gt 0) {
        $list = ($conflicts | Select-Object -First 20) -join "`n         "
        Stop-Chaos -Message "$($conflicts.Count) file(s) collided with different content while staging $Source." `
                   -Hint "Two published apps disagree on these files:`n         $list`n       Align the package versions (usually the Windows App SDK or a shared transitive dependency) rather than letting one overwrite the other."
    }

    Write-ChaosDetail "staged  $copied file(s) from $(Split-Path -Leaf $srcRoot)"
}

function Remove-ChaosDirectory {
    param([Parameter(Mandatory)][string]$Path)
    if (Test-Path $Path) {
        Write-ChaosDetail "removing $Path"
        Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
    }
}

function Get-ChaosVersion {
    <#  Read <Version> out of windows/Directory.Build.props.  #>
    [CmdletBinding()]
    param()
    $paths = Get-ChaosPaths
    $props = Join-Path $paths.Windows 'Directory.Build.props'
    if (-not (Test-Path $props)) { return '0.0.0' }
    $xml = [xml](Get-Content -Raw $props)
    $node = $xml.SelectSingleNode('//PropertyGroup/Version')
    if ($node -and $node.InnerText) { return $node.InnerText.Trim() }
    return '0.0.0'
}

Export-ModuleMember -Function `
    Get-ChaosLayout, Get-ChaosPaths, Get-ChaosVersion, `
    Write-ChaosStep, Write-ChaosDetail, Write-ChaosWarning, Stop-Chaos, Invoke-ChaosNative, `
    Assert-ChaosWindows, Assert-ChaosDotnet, Assert-ChaosSolution, Test-ChaosElevated, `
    Get-ChaosVerifiedFile, Copy-ChaosTree, Remove-ChaosDirectory
