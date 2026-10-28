param(
  [Parameter(Mandatory=$true)][ValidateSet('uno','esp32cam')][string]$Board,
  [Parameter(Mandatory=$true)][ValidatePattern('^COM[0-9]+$')][string]$Port,
  [ValidateSet('2','0')][string]$Uart = '2'
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$python = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Nejdrive spust prepare.ps1.' }
if (Test-Path 'data/system-ca.pem') { $env:REQUESTS_CA_BUNDLE = Join-Path $PSScriptRoot 'data/system-ca.pem' }
$target = $Board
if ($Board -eq 'esp32cam' -and $Uart -eq '0') { $target = 'esp32cam_uart0' }
Write-Host 'Pred nahravanim: vypnute baterie, odpojeny seriovy kabel mezi deskami, pripojena pouze vybrana deska pres USB.'
Write-Host "Nahravam $target na $Port. Navod: docs/NAHRANI_FIRMWARU.md"
& $python -m platformio run -e $target -t upload --upload-port $Port
if ($LASTEXITCODE -ne 0) { throw 'Nahravani se nepodarilo. Kabel mezi deskami zatim nezapojuj.' }
Write-Host 'SUCCESS. Odpoj USB. Propojovaci kabel vrat az po uspesnem nahrani OBOU desek.'
