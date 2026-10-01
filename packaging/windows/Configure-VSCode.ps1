$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
$picker = New-Object System.Windows.Forms.FolderBrowserDialog
$picker.Description = 'Select your AM32 firmware source folder (containing Inc and Src)'
if ($picker.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) { exit 0 }
$repo = $picker.SelectedPath
$target = Read-Host 'Renode firmware target [VIMDRONES_L431]'
if (!$target) { $target = 'VIMDRONES_L431' }
$cygwin = Read-Host 'Cygwin folder [C:\cygwin64]'
if (!$cygwin) { $cygwin = 'C:\cygwin64' }
$cli = Join-Path (Split-Path $PSScriptRoot -Parent) 'ESCSim-cli.exe'
& $cli debug workspace --repo $repo --target $target --cygwin $cygwin
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host 'Open ESCSim.code-workspace in that folder with VS Code.'
Write-Host 'Install the Microsoft C/C++ extension, then choose ESCSim SITL or ESCSim Renode in Run and Debug.'
