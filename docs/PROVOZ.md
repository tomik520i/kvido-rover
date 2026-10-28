# Instalace a provoz

Příklady používají localhost a rezervovanou doménu `rover.example.com`.
Nejsou to přístupy k existujícímu autu. Pracuj z kořene repozitáře.

## Python a panel

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-simulator.txt
$env:ROVER_LLM_URL = 'http://127.0.0.1:8080'
$env:ROVER_VERITY_URL = 'http://127.0.0.1:3700'
.\.venv\Scripts\python.exe tools/run.py --host 127.0.0.1 --port 28741
```

Na Linuxu nahraď `.venv\Scripts\python.exe` za `.venv/bin/python` a
proměnné nastav pomocí `export`. Základní panel vyžaduje
`requirements.txt`; radar a obrazové experimenty navíc
`requirements-simulator.txt`. Certifikační chyby při instalaci řeš
důvěryhodným CA bundlem, nevypínej ověřování HTTPS.

Panel začne s `simulation=true`, bez spárovaného auta. Pro skutečný
simulovaný byt použij `tools/run_simulator.py` podle README. Pro fyzické
auto nejprve dokonči firmware a párování. Po startu vznikají:

- `data/access.key`: klíč pro přihlášení do kokpitu.
- `data/relay.key`: samostatný klíč transportu ESP → server.
- `data/settings.json`: lokální nastavení včetně párování.
- `data/memory.sqlite3`, JSON stavů a diagnostika: soukromá runtime data.
- `data/openai.key`: vlastní OpenAI klíč, pokud jej uložíš přes panel.

Proměnná `ROVER_DATA` přesune datový adresář. `.env.example` pouze
dokumentuje proměnné; aplikace `.env` sama nenačítá. Klíče si prohlížej
lokálně, nekopíruj je do issue, screenshotu ani commitu.

## Párování a internet

1. Připrav obě desky podle [návodu](NAHRANI_FIRMWARU.md).
2. Připoj telefon k `Kvido-Setup`, otevři `http://192.168.4.1`, přihlas se
   údaji z lokálního `data/pairing.txt` a vyber Wi-Fi 2,4 GHz.
3. ESP nabídne svou LAN adresu s portem a soubor připojení. Soubor obsahuje
   klíč auta a patří pouze do soukromého nastavení kokpitu.
4. V kokpitu nastav LAN adresu a klíč, zvol transport a vypni simulaci
   až pro vědomou zkoušku skutečného auta.
5. Pro provoz mimo LAN vystav kokpit přes vlastní HTTPS reverzní proxy.
   Nastav relay URL, token a důvěryhodnou CA ve vlastním
   `firmware/esp32cam/relay_secrets.h` a sestav ESP firmware.
6. V nastavení kokpitu vyber relay. Nejdřív ověř kameru a telemetrii,
   potom ruční krátký pohyb a až následně autonomní úkol.

Projekt umí veřejný prefix `/kvido/`; proxy předává tuto cestu včetně
WebSocket endpointu `/kvido/device-ws`. Musí zvládat dlouhé AI/hlasové
odpovědi a průběžný kamerový přenos. Konkrétní konfigurace domácí HAProxy
ani ostatní weby původní instalace nejsou součástí exportu. V provozu
omez přístup na backend, který by jinak obcházel proxy.

## Hlas

Aktuální instalace používá **OmniVoice TTS** (clone, čeština) a
**Whisper large-v3-turbo přes faster-whisper / CTranslate2** pro STT.
Podrobnosti, ověřené nastavení a tok audia jsou v [HLAS.md](HLAS.md).
STT a TTS jsou proxy na samostatnou službu kompatibilní s Verity:

- `POST /api/stt`: WAV tělo, volitelně query `live=1` a `final=1`, odpověď JSON.
- `POST /api/tts`: JSON `{"text":"Ahoj","emotion":"neutral"}`, odpověď WAV.

Základ této služby nastavuje `ROVER_VERITY_URL`. Bez ní funguje text,
ale hlasové služby budou nedostupné. Mikrofon a reproduktor jsou v telefonu
nebo počítači, nikoli dodatečný hardware na autě. Prohlížeč musí mít povolený
mikrofon a zabezpečený kontext; pro vzdálený telefon používej důvěryhodné HTTPS.

## Radar a novější autonomní alfa

Stáhni zvlášť model `onnx/model.onnx` z veřejného repozitáře
`onnx-community/depth-anything-v2-small` a ulož jej jako
`data/models/depth-anything-v2-small/model.onnx`. Modelové váhy se do Gitu
nepřidávají. Ověř si jejich zdroj a licenci; odkazy jsou v THIRD_PARTY.md.

Ve druhém terminálu, s běžícím kokpitem:

```powershell
$env:ROVER_LLM_URL = 'http://127.0.0.1:8080'
.\.venv\Scripts\python.exe radar_service/radar_service.py --base http://127.0.0.1:28741/ --key data/access.key --ca "" --interval 10
```

Radar naslouchá na `127.0.0.1:27261`. Na samotném radarovém portu není
autentizace, proto jej nezpřístupňuj do veřejné sítě. Kokpit poskytuje
přihlášenou cestu `/radar/` a proxy pro řízení alfy. Při HTTPS backendu
předej `--ca` s důvěryhodným CA souborem, pokud jde o vlastní CA.
Pro vlastní konfiguraci jsou dostupné `--help`, `--model`, `--data`,
`--gemma`, `--threads` a `--interval`.

Kalibrační konstanty v `server/motion_calibration.py` a
`server/camera_radar.py` jsou výchozí hodnoty z jednoho kusu auta.
Zkontroluj je podle vlastního povrchu, napájení a náklonu kamery.
Vyšší PWM ani delší čas nejsou univerzálním řešením chyb mapy.

## Diagnostika a restart

Nejdřív rozlišuj: obraz, čerstvost UNO telemetrie, Wi-Fi ESP, relay a stav
AI požadavku. Radarový dashboard ukazuje stáří snímku a výpadky. Logy a
uložené snímky zůstávají v ignorovaných datových adresářích; při sdílení
vytvoř zvlášť anonymizovaný výřez.

Před restartem backendu nebo flashováním zastav úkol v panelu. Restartuj
jen procesy této instalace, ne souběžné aplikace používající stejnou Gemmu.
Po návratu ověř čerstvé údaje a skutečný stav auta. Explicitní STOP
operátora se nemá automaticky rušit pokusem o reconnect.
