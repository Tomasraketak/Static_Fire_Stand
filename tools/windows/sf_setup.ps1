# =====================================================================
#  Static Fire Stand - one-time setup on Windows
#
#  Run from cmd (one line):
#    powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/Tomasraketak/Static_Fire_Stand/main/tools/windows/sf_setup.ps1 | iex"
#
#  What it does (safe to run again at any time):
#    1. installs Git and Python 3.12 with winget, if they are missing;
#    2. puts the repository into  <Desktop>\Static Fire Stand  as a git
#       clone (no ZIP). An existing folder that is NOT a git clone is kept,
#       renamed to "Static Fire Stand (stara kopie <date>)";
#    3. creates the "Static Fire Stand" icon on the desktop. It runs
#       sf_launch.ps1, which on every start updates the code, Python and
#       the packages, then opens the program.
#
#  ASCII only on purpose: Windows PowerShell 5.1 misreads UTF-8 without BOM.
# =====================================================================
# Another folder: set SF_TARGET first, e.g.  set SF_TARGET=D:\Static Fire Stand
# (no param() block: this script is also run through "irm | iex").
$ErrorActionPreference = "Stop"
$Target = $env:SF_TARGET

$RepoUrl = "https://github.com/Tomasraketak/Static_Fire_Stand.git"
$AppDir  = Join-Path $env:LOCALAPPDATA "StaticFireStand"

function Say($msg) { Write-Host ">> $msg" -ForegroundColor Cyan }

function Refresh-Path {
    $m = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $u = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$m;$u"
}

function Have($cmd) { return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

function Find-Python312 {
    if (Have "py") {
        try {
            $p = (& py -3.12 -c "import sys; print(sys.executable)" 2>$null)
            if ($LASTEXITCODE -eq 0 -and $p -and (Test-Path $p)) { return $p.Trim() }
        } catch {}
    }
    $c = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
    if (Test-Path $c) { return $c }
    $c = Join-Path $env:ProgramFiles "Python312\python.exe"
    if (Test-Path $c) { return $c }
    return $null
}

# ---- target folder ----------------------------------------------------------
if (-not $Target) {
    $od = Join-Path $env:USERPROFILE "OneDrive\Desktop"
    $desk = if (Test-Path $od) { $od } else { [Environment]::GetFolderPath("Desktop") }
    $Target = Join-Path $desk "Static Fire Stand"
}
New-Item -ItemType Directory -Force -Path $AppDir | Out-Null

# ---- tools ----------------------------------------------------------------------
if (-not (Have "winget") -and (-not (Have "git") -or -not (Find-Python312))) {
    throw "winget neni k dispozici. Nainstalujte 'App Installer' z Microsoft Store, nebo rucne Git a Python 3.12."
}
if (-not (Have "git")) {
    Say "Instaluji Git..."
    winget install -e --id Git.Git --silent --accept-package-agreements --accept-source-agreements
    Refresh-Path
    if (-not (Have "git")) { $env:Path += ";$env:ProgramFiles\Git\cmd" }
}
if (-not (Find-Python312)) {
    Say "Instaluji Python 3.12..."
    winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
    Refresh-Path
}
$py = Find-Python312
if (-not $py) { throw "Python 3.12 se nepodarilo najit ani po instalaci." }
Say "Python: $py"

# ---- the repository ---------------------------------------------------------------
if (Test-Path (Join-Path $Target ".git")) {
    Say "$Target je uz git klon."
} else {
    if ((Test-Path $Target) -and (Get-ChildItem -Force $Target | Select-Object -First 1)) {
        $bak = "$Target (stara kopie $(Get-Date -Format 'yyyyMMdd-HHmm'))"
        Say "Slozka $Target neni git klon - prejmenovavam ji na '$bak' (nic se nemaze)."
        Rename-Item -Path $Target -NewName (Split-Path $bak -Leaf)
    }
    Say "Stahuji $RepoUrl -> $Target"
    git clone $RepoUrl $Target
    if ($LASTEXITCODE -ne 0) { throw "git clone selhal." }
}

# ---- config for the launcher ------------------------------------------------------------
@{ repo = $Target } | ConvertTo-Json | Set-Content -Encoding ASCII (Join-Path $AppDir "config.json")

# The launcher and the icon run from %LOCALAPPDATA%\StaticFireStand, not from
# the repository: git may rewrite the repository while the launcher is running,
# and OneDrive may keep a file "online only". The launcher copies its own
# newest version over after every update.
$win = Join-Path $Target "tools\windows"
Copy-Item -Force (Join-Path $win "sf_launch.ps1") (Join-Path $AppDir "sf_launch.ps1")
Copy-Item -Force (Join-Path $win "sf_stand.ico")  (Join-Path $AppDir "sf_stand.ico")

# ---- the icon on the desktop -----------------------------------------------------------------
$desktop = [Environment]::GetFolderPath("Desktop")
$lnk = Join-Path $desktop "Static Fire Stand.lnk"
$sh = New-Object -ComObject WScript.Shell
$s = $sh.CreateShortcut($lnk)
$s.TargetPath       = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$s.Arguments        = "-NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $AppDir 'sf_launch.ps1')`""
$s.WorkingDirectory = $Target
$s.IconLocation     = (Join-Path $AppDir "sf_stand.ico") + ",0"
$s.Description      = "Static Fire Stand: aktualizuje program, pak ho spusti"
$s.Save()
Say "Ikona na plose: $lnk"

Say "Prvni spusteni (vytvori prostredi Pythonu, to chvili trva)..."
& (Join-Path $AppDir "sf_launch.ps1")
