# Obě desky a nahrávání firmwaru

Hardware: KUONGSHUN AD176, řídicí UNO a ESP32-CAM. Zapojení ověř podle
konkrétní revize stavebnice. Tento projekt používá vlastní firmware obou
desek; originální firmware a firemní ZIP balíky nejsou v repozitáři.

## Příprava bez nahrání

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt platformio==6.2.0
.\.venv\Scripts\python.exe tools/init_device.py
.\.venv\Scripts\python.exe -m platformio run -e uno -e esp32cam_uart0
```

`init_device.py` vytvoří náhodné lokální heslo/klíč v
`firmware/esp32cam/device_secrets.h`, soukromý `data/pairing.txt` a kopii
vzorového relay nastavení, pokud soubory ještě neexistují. Existující klíče
nepřepíše. Relay je v nové instalaci vypnutý. Chceš-li internetový provoz,
doplň vlastní `relay_secrets.h`: URL kokpitu, WSS adresu, token z backendu
a důvěryhodnou veřejnou kořenovou CA. Nastav `KVIDO_RELAY_ENABLED 1`.

`esp32cam_uart0` používá společný sériový port s UNO, stejně jako testovaný
kus auta. Základní `esp32cam` používá UART2 — není záměnný bez správného
zapojení. Profil `esp32cam_bench` je servisní diagnostika. Profil
`uno_legacy` má starší pohybové limity; nemíchej profily bez kontroly
schopností, které UNO hlásí v telemetrii.

## Nahrávání přes USB

1. Zastav úkol a vypni bateriové napájení.
2. Odpoj sériový propojovací kabel mezi UNO a ESP32. U UART0 by druhá deska
   rušila nahrávání i čtení diagnostiky.
3. Připoj přes USB pouze desku, kterou nahráváš. Najdi její aktuální COM port:

```powershell
.\ports.ps1
```

4. Nahraj UNO, potom při odpojeném USB přepoj kabel na ESP32 a nahraj ESP.
   `COM7` v příkladu nahraď skutečným portem dané desky:

```powershell
.\upload.ps1 -Board uno -Port COM7
.\upload.ps1 -Board esp32cam -Port COM7 -Uart 0
```

Tyto dva příkazy se nespouštějí současně. Pokud USB adaptér ESP vyžaduje
ruční boot režim, použij postup výrobce své desky/adaptéru; zapojení pinů
nelze bezpečně odhadnout pro všechny revize. Před prvním přehráním si
uschovej původní firmware a nastavení svého kusu.

5. Po úspěšném nahrání odpoj USB. Při vypnutých bateriích vrať původní
   propojovací kabel. První kontrolu telemetrie a směru motorů udělej s koly
   nad zemí; teprve potom testuj na volné podlaze s dostupným STOPem.

ESP nabízí nastavení Wi-Fi, ale hlavní AI kokpit běží na Python serveru.
Změny serverových promptů, mapy a paměti nevyžadují další flash desek.

Firmware obsahuje také LAN OTA aktualizaci ESP s ověřením obrazu a
podporou dvou oddílů. Automatické zotavení z vadného obrazu nebylo plně
ověřeno. Pro první instalaci použij výše uvedený USB postup; soukromé
jednorázové OTA/deploy skripty původní instalace nejsou v tomto exportu.
