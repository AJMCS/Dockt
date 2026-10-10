# Adds a "Dockt" shortcut (with the Dockt icon) to the Desktop and Start menu.
# Run once:  powershell -ExecutionPolicy Bypass -File desktop\install-shortcut.ps1
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $py)) { Write-Host "Run desktop\Dockt.bat once first (it sets up .venv)."; exit 1 }
& (Join-Path $root ".venv\Scripts\python.exe") -c "import sys; sys.path.insert(0, r$PSScriptRoot); import dockt_desktop as d; d.make_icon()"
$shell = New-Object -ComObject WScript.Shell
foreach ($dir in @([Environment]::GetFolderPath("Desktop"), (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"))) {
  $lnk = $shell.CreateShortcut((Join-Path $dir "Dockt.lnk"))
  $lnk.TargetPath = $py
  $lnk.Arguments = "`"$PSScriptRoot\dockt_desktop.py`""
  $lnk.WorkingDirectory = $root
  $lnk.IconLocation = (Join-Path $PSScriptRoot "dockt.ico")
  $lnk.Description = "Dockt: your workday, remembered. 100% on-device."
  $lnk.Save()
}
Write-Host "Dockt shortcuts added to the Desktop and Start menu."
