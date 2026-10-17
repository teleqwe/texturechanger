# Builds texturechanger.exe and copies it to the Desktop. Run: powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
python test_texturechanger.py
if ($LASTEXITCODE -ne 0) { throw 'Tests failed, not building.' }
python -m PyInstaller --noconfirm --onefile --windowed --name texturechanger --add-data 'ui.html;.' --icon icon.ico texturechanger.py
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }
$desktop = [Environment]::GetFolderPath('Desktop')
# Close any running copy so it can be replaced (unapplied changes in it are lost).
Get-Process -Name texturechanger -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 500  # let Windows release the file
Copy-Item 'dist/texturechanger.exe' $desktop -Force
Write-Output "Copied to $desktop/texturechanger.exe"
