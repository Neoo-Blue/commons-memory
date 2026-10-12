# Commons one-line installer (Windows PowerShell 5+/7):
#   irm https://raw.githubusercontent.com/Neoo-Blue/commons-memory/main/install.ps1 | iex
# Options (env): $env:COMMONS_VERSION='v0.7.1'  $env:COMMONS_EXTRAS='http,embed'  $env:COMMONS_NO_INIT='1'  $env:COMMONS_INSTALLER='uv|pipx|pip'
$ErrorActionPreference = 'Stop'
$Repo = 'Neoo-Blue/commons-memory'
$Extras = if ($env:COMMONS_EXTRAS) { $env:COMMONS_EXTRAS } else { 'http,embed' }
function Say($m) { Write-Host "commons: $m" -ForegroundColor Magenta }
function Have($c) { [bool](Get-Command $c -ErrorAction SilentlyContinue) }

$Ver = $env:COMMONS_VERSION
if (-not $Ver) { try { $Ver = (Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/latest").tag_name } catch { $Ver = $null } }
if ($Ver) {
  $N = $Ver.TrimStart('v')
  $Spec = "commons-memory[$Extras] @ https://github.com/$Repo/releases/download/$Ver/commons_memory-$N-py3-none-any.whl"
} else { Say 'could not resolve latest release; installing from main'; $Spec = "commons-memory[$Extras] @ git+https://github.com/$Repo" }

$Py = $null
foreach ($c in 'py', 'python', 'python3') {
  if (Have $c) { & $c -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" 2>$null; if ($LASTEXITCODE -eq 0) { $Py = $c; break } }
}
$Method = $env:COMMONS_INSTALLER
if (-not $Method) { $Method = if (Have 'uv') { 'uv' } elseif (Have 'pipx') { 'pipx' } elseif ($Py) { 'pip' } else { 'uv-bootstrap' } }
Say "installing $(if ($Ver) { $Ver } else { 'main' }) via $Method"
switch ($Method) {
  'uv-bootstrap' {
    Say 'no uv/pipx/Python 3.10+ found; installing uv (https://astral.sh/uv)'
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    uv tool install --force --python 3.12 $Spec
  }
  'uv'   { uv tool install --force $Spec }
  'pipx' { pipx install --force $Spec }
  'pip'  {
    $Venv = if ($env:COMMONS_VENV) { $env:COMMONS_VENV } else { "$env:USERPROFILE\.commons\venv" }
    & $Py -m venv $Venv
    & "$Venv\Scripts\python.exe" -m pip install -q --upgrade pip
    & "$Venv\Scripts\python.exe" -m pip install -q $Spec
    $Bin = "$env:USERPROFILE\.local\bin"; New-Item -ItemType Directory -Force $Bin | Out-Null
    Set-Content "$Bin\commons.cmd" "@`"$Venv\Scripts\commons.exe`" %*"
    $env:Path = "$Bin;$env:Path"
  }
  default { throw "unknown COMMONS_INSTALLER=$Method" }
}
if (-not (Have 'commons')) { throw "installed, but 'commons' is not on PATH; add $env:USERPROFILE\.local\bin to PATH" }
Say "installed $(commons --version)"
if ($env:COMMONS_NO_INIT -ne '1') { commons init | Out-Null; Say 'database ready: ~\.commons\commons.db' }
@'

Next steps
  1. Connect an AI:      commons connect claude --platform claude-code     (or cursor, codex, gemini, generic)
  2. Try it:             commons remember "Commons is installed"; commons search "installed"
  3. Dashboard:          commons dashboard            -> http://127.0.0.1:8766
  4. Team / other boxes: commons serve --transport http --host <tailnet-ip> --port 8770
  5. Paste PROMPT.md into any AI: https://github.com/Neoo-Blue/commons-memory/blob/main/PROMPT.md
'@ | Write-Host
