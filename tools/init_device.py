"""Create per-device secrets once. Never commit pairing.txt or device_secrets.h."""
from pathlib import Path
import secrets
import json

root=Path(__file__).resolve().parents[1]
header=root/'firmware/esp32cam/device_secrets.h'
data=root/'data';data.mkdir(exist_ok=True)
if header.exists():
    print('Device secrets already exist; unchanged.')
else:
    password=secrets.token_urlsafe(12)
    key=secrets.token_hex(24)
    header.write_text('#pragma once\n#define SETUP_PASSWORD '+json.dumps(password)+'\n#define WIFI_AP_PASSWORD '+json.dumps(password)+'\n#define DEVICE_KEY '+json.dumps(key)+'\n',encoding='utf-8')
    header.chmod(0o600)
    (data/'pairing.txt').write_text('Wi-Fi: Kvido-Setup\nHeslo Wi-Fi: '+password+'\nWeb: http://192.168.4.1\nUzivatel: admin\nHeslo webu: '+password+'\nKlic auta: '+key+'\n',encoding='utf-8')
    (data/'pairing.txt').chmod(0o600)
    print('Created firmware/esp32cam/device_secrets.h and data/pairing.txt.')

relay = root/'firmware/esp32cam/relay_secrets.h'
if not relay.exists():
    relay.write_text((relay.parent/'relay_secrets.example.h').read_text(encoding='utf-8'), encoding='utf-8')
    relay.chmod(0o600)
    print('Created relay_secrets.h with relay disabled; configure your own server before enabling.')
