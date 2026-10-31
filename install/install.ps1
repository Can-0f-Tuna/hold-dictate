# hold-dictate installer for Windows.  Usage (PowerShell): irm <site>/install.ps1 | iex
# Running it again updates to the latest release.
$ErrorActionPreference = "Stop"
$Repo = "Can-0f-Tuna/hold-dictate"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "==> Installing uv (Python tool installer)"
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:Path = "$env:USERPROFILE\.local\bin;$env:USERPROFILE\.cargo\bin;$env:Path"
}

if ($env:HOLD_DICTATE_SOURCE) {
    $Source = $env:HOLD_DICTATE_SOURCE
} else {
    $Source = "git+https://github.com/$Repo"
    try {
        $Tag = (Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/latest").tag_name
        if ($Tag) { $Source = "$Source@$Tag" }
    } catch { }
}

# A running copy locks its files; stop it before reinstalling.
Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
    Where-Object { $_.CommandLine -like "*hold_dictate*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Write-Host "==> Installing hold-dictate $Tag"
uv tool install --force $Source
if ($LASTEXITCODE -ne 0) { throw "install failed" }

Write-Host "==> Starting hold-dictate (it also starts at every login; look for its icon next to the clock)"
hold-dictate service install
if (-not $env:HOLD_DICTATE_NO_SETUP) {
    Write-Host "==> Opening the settings in your browser. Add your API key there."
    hold-dictate settings --wait 10
}
