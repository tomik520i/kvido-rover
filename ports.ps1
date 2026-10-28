$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
& '.venv/Scripts/python.exe' -m serial.tools.list_ports -v
