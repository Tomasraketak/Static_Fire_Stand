# =====================================================================
#  Static Fire Stand - launcher behind the "Static Fire Stand" desktop icon
#
#  Installed by sf_setup.ps1 into %LOCALAPPDATA%\StaticFireStand and run
#  from there. On every start:
#    1. updates the repository to the newest origin/main (git, no ZIP).
#       Local edits to tracked files are put aside with "git stash" first,
#       never thrown away. Measurements (tools\results) are not touched:
#       git ignores that folder;
#    2. once a day updates Python 3.12 itself (winget);
#    3. keeps a private Python environment in %LOCALAPPDATA%\StaticFireStand\venv
#       and upgrades the packages in tools\requirements.txt;
#    4. starts the GUI (tools\sf_gui.py).
#  Without internet it skips 1-3 and starts what is there.
#  Log: %LOCALAPPDATA%\StaticFireStand\launch.log
#
#  ASCII only on purpose: Windows PowerShell 5.1 misreads UTF-8 without BOM.
# =====================================================================
$ErrorActionPreference = "Continue"
$AppDir = Join-Path $env:LOCALAPPDATA "StaticFireStand"
$Venv   = Join-Path $AppDir "venv"
$VPy    = Join-Path $Venv "Scripts\python.exe"
$VPyW   = Join-Path $Venv "Scripts\pythonw.exe"
$Stamp  = Join-Path $AppDir "last_python_upgrade.txt"
try { Start-Transcript -Path (Join-Path $AppDir "launch.log") -Force | Out-Null } catch {}
$Host.UI.RawUI.WindowTitle = "Static Fire Stand - aktualizace"

function Say($msg)  { Write-Host ">> $msg" -ForegroundColor Cyan }
function Warn($msg) { Write-Host "!! $msg" -ForegroundColor Yellow }
function Have($cmd) { return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
function Fail($msg) {
    Write-Host "CHYBA: $msg" -ForegroundColor Red
    try { Stop-Transcript | Out-Null } catch {}
    Read-Host "Enter = zavrit"
    exit 1
}

$m = [Environment]::GetEnvironmentVariable("Path", "Machine")
$u = [Environment]::GetEnvironmentVariable("Path", "User")
$env:Path = "$m;$u"

$cfgFile = Join-Path $AppDir "config.json"
if (-not (Test-Path $cfgFile)) { Fail "Chybi $cfgFile - spustte nejdriv sf_setup.ps1." }
$cfg = Get-Content $cfgFile -Raw | ConvertFrom-Json
$Repo = $cfg.repo
if (-not (Test-Path (Join-Path $Repo ".git"))) { Fail "$Repo neni git klon - spustte znovu sf_setup.ps1." }

# ---- 1. the repository -----------------------------------------------------
$online = $true
if (Have "git") {
    Say "Aktualizuji $Repo"
    git -C $Repo fetch --prune origin
    if ($LASTEXITCODE -ne 0) {
        Warn "Nelze stahnout (offline?) - pouzivam to, co je na disku."
        $online = $false
    } else {
        $dirty = git -C $Repo status --porcelain --untracked-files=no
        if ($dirty) {
            $msg = "auto-zaloha pred aktualizaci $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
            git -C $Repo stash push -m $msg | Out-Null
            Warn "Upravene soubory byly odlozeny do 'git stash' ($msg). Zpet: git stash pop"
        }
        $br = (git -C $Repo rev-parse --abbrev-ref HEAD).Trim()
        if ($br -ne "main") { git -C $Repo checkout -q main 2>$null; if ($LASTEXITCODE -ne 0) { git -C $Repo checkout -q -B main origin/main } }
        git -C $Repo merge --ff-only -q origin/main
        if ($LASTEXITCODE -ne 0) {
            $bak = "zaloha-$(Get-Date -Format 'yyyyMMdd-HHmm')"
            git -C $Repo branch $bak | Out-Null
            git -C $Repo reset -q --hard origin/main
            Warn "Lokalni commity odlozeny do vetve '$bak', slozka je ted presne origin/main."
        }
        Say ("   " + (git -C $Repo log -1 --format="%h %ad %s" --date=short))
    }
} else { Warn "Git neni nainstalovany - kod se neaktualizuje (spustte sf_setup.ps1)." }

# The launcher and the icon update themselves for the NEXT start.
$win = Join-Path $Repo "tools\windows"
foreach ($f in "sf_launch.ps1", "sf_stand.ico") {
    $src = Join-Path $win $f
    if (Test-Path $src) { Copy-Item -Force $src (Join-Path $AppDir $f) -ErrorAction SilentlyContinue }
}

# ---- 2. Python itself, once a day ------------------------------------------
$today = Get-Date -Format "yyyy-MM-dd"
$last  = if (Test-Path $Stamp) { (Get-Content $Stamp -Raw).Trim() } else { "" }
if ($online -and (Have "winget") -and $last -ne $today) {
    Say "Kontroluji novou verzi Pythonu 3.12 (jednou denne)..."
    winget upgrade -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements --disable-interactivity | Out-Null
    Set-Content -Encoding ASCII $Stamp $today
}

function Find-Python312 {
    if (Have "py") {
        $p = & py -3.12 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $p -and (Test-Path $p.Trim())) { return $p.Trim() }
    }
    foreach ($c in (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
                   (Join-Path $env:ProgramFiles "Python312\python.exe")) {
        if (Test-Path $c) { return $c }
    }
    return $null
}

# ---- 3. the private environment and the packages -----------------------------
$base = Find-Python312
$ok = (Test-Path $VPy) -and ((& $VPy -c "import sys; print(sys.version_info[:2]==(3,12))" 2>$null) -eq "True")
if (-not $ok) {
    if (-not $base) { Fail "Python 3.12 nenalezen - spustte sf_setup.ps1." }
    Say "Vytvarim prostredi Pythonu v $Venv"
    & $base -m venv --clear $Venv
    if ($LASTEXITCODE -ne 0) { Fail "python -m venv selhal." }
}
if ($online) {
    Say "Aktualizuji balicky (pip)..."
    & $VPy -m pip install --upgrade --disable-pip-version-check -q pip
    & $VPy -m pip install --upgrade --upgrade-strategy eager --disable-pip-version-check -q `
        -r (Join-Path $Repo "tools\requirements.txt")
    if ($LASTEXITCODE -ne 0) { Warn "Nektere balicky se nepodarilo aktualizovat." }
}

# ---- 4. start the GUI ----------------------------------------------------------
$gui = Join-Path $Repo "tools\sf_gui.py"
if (-not (Test-Path $gui)) { Fail "Nenalezeno: $gui" }
& $VPy -c "import numpy, pandas, matplotlib, openpyxl, serial, tkinter" 2>$null
if ($LASTEXITCODE -ne 0) { Fail "V prostredi chybi balicky (numpy/pandas/matplotlib/openpyxl/pyserial/tkinter) a nejde je stahnout (offline?)." }

Say "Spoustim Static Fire Stand..."
# pythonw has no console, so a crash at start-up would be silent: its stderr
# goes to gui_error.log and if the GUI dies within the first seconds the
# window stays open and shows why.
$errLog = Join-Path $AppDir "gui_error.log"
$p = Start-Process -FilePath $VPyW -ArgumentList "`"$gui`"" -WorkingDirectory (Join-Path $Repo "tools") `
        -RedirectStandardError $errLog -PassThru
Start-Sleep -Seconds 6
if ($p.HasExited -and $p.ExitCode -ne 0) {
    Write-Host ""
    Write-Host "GUI spadlo hned po startu (kod $($p.ExitCode)). Chyba:" -ForegroundColor Red
    if (Test-Path $errLog) { Get-Content $errLog -Tail 40 | ForEach-Object { Write-Host $_ } }
    Write-Host ""
    Write-Host "Ulozeno v: $errLog" -ForegroundColor Yellow
    Fail "Program se nespustil."
}
try { Stop-Transcript | Out-Null } catch {}
