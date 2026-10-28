$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path '.venv/Scripts/python.exe')) {
  python -m venv .venv
  if ($LASTEXITCODE -ne 0) { throw 'Je potreba Python 3.11 nebo novejsi.' }
}
& '.venv/Scripts/python.exe' -c "import ssl,pathlib; p=pathlib.Path('data'); p.mkdir(exist_ok=True); (p/'system-ca.pem').write_text(''.join(ssl.DER_cert_to_PEM_cert(c) for c in ssl.create_default_context().get_ca_certs(binary_form=True)),encoding='ascii')"
$env:PIP_CERT = Join-Path $PSScriptRoot 'data/system-ca.pem'
$env:REQUESTS_CA_BUNDLE = $env:PIP_CERT
& '.venv/Scripts/python.exe' -m pip install -r requirements.txt 'platformio==6.2.0'
if ($LASTEXITCODE -ne 0) { throw 'Instalace zavislosti selhala.' }
& '.venv/Scripts/python.exe' tools/init_device.py
if ($LASTEXITCODE -ne 0) { throw 'Priprava klicu selhala.' }
& '.venv/Scripts/python.exe' -m platformio run
if ($LASTEXITCODE -ne 0) { throw 'Sestaveni firmwaru selhalo.' }
Write-Host 'Pripraveno. Zadne zarizeni nebylo prepsano.'
