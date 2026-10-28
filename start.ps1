param([switch]$LanHttps,[string]$Ip)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path '.venv/Scripts/python.exe')) { python -m venv .venv }
& '.venv/Scripts/python.exe' -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Instalace závislostí selhala.' }
if ($LanHttps) {
  if (-not $Ip) { throw 'Pouzij -LanHttps -Ip 192.168.x.x (adresa tohoto pocitace).' }
  & '.venv/Scripts/python.exe' tools/run.py --host $Ip --https --ip $Ip
} else {
  & '.venv/Scripts/python.exe' tools/run.py
}
