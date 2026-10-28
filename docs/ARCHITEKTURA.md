# Jak systém funguje

## Oddělení desek a serveru

UNO přijímá omezené povely po sériovém propojení, ovládá pohon a servo a
vrací ultrazvukovou telemetrii. ESP32-CAM poskytuje kameru, světlo, Wi-Fi
a transport. Prompty, AI inference, paměť a plánování běží na serveru.
Změna promptu tedy nevyžaduje přeprogramování desek.

LAN režim používá lokální HTTP API ESP. Relay režim navazuje spojení
**z ESP směrem k serveru** přes WSS. U cizí Wi-Fi tak není potřeba otevírat
port na routeru auta. Webový kokpit komunikuje s backendem přes HTTPS;
backend přeposílá povely a obrazy. Reverzní proxy musí podporovat WebSocket.

## Dvě hlavní cesty řízení

`server/task_runner.py` řeší postupné kroky klasického AI úkolu. Pozorování
vede k rozhodnutí, omezenému povelu a následnému ověření obrázku před/po.
Pokusy se zapisují do historie, takže opakovaný neúčinný krok lze odmítnout.

Novější kamerová alfa používá `server/alpha_runner.py`,
`radar_service/radar_service.py` a `server/autopilot_core.py`. Radarová
služba navrhuje akci; vlastní povel stále provádí backend kokpitu přes
kontroly transportu a fyzické limity. Stránka nemusí dělat jednotlivé kroky
JavaScriptovým časovačem. Ruční řízení má vlastní pravidla přítomnosti
operátora; nelze je automaticky zaměňovat s během serverové alfy.

Typický cyklus alfy:

1. Ve stoje získá čerstvou telemetrii a snímek.
2. Odhadne volnou podlahu, aktualizuje mapu, AI zhodnotí cíl a scénu.
3. Když je potřeba rozhled, otáčí auto a ukládá jednotlivé pohledy.
4. Vybere směr s využitím pozorování, mapy a paměti míst.
5. Porovná aktuální pohled s uloženým pohledem vybraného směru.
6. Provede omezený pohyb, zaznamená skutečně přijatý povel a znovu pozoruje.

Pokud AI dlouho čeká ve frontě, musí se před použitím staršího rozhodnutí
znovu ověřit scéna. Výpadek transportu se řeší čekáním a návratem do běhu;
pohybové povely mají vlastní časové limity. Explicitní STOP operátora není
totéž co dočasná ztráta spojení. Restart procesu může obnovit uložený stav,
ale není zárukou automatického bezpečného pokračování za všech okolností.

## Radar a mapa

„Radar“ je pracovní název kamerového odhadu, nikoli skutečný radar nebo
LiDAR. Depth Anything V2 Small vytvoří relativní hloubku. Modul
`camera_radar.py` hledá přechod překážky do podlahy a pomocí kalibrace pro
náklon kamery převede obraz na přibližné vzdálenosti ve 32 směrech.

`rover_map.py` skládá lokální mřížku obsazenosti. Pohyb odhaduje z přijatých
povelů a kalibrace; `scan_matching.py` hledá místní shodu nového skenu s
dosavadní mapou. Nejasné zarovnání se nesmí automaticky považovat za volné
místo. Mapa ovlivňuje výběr směru, ale její poloha může driftovat.
`visual_heading.py` a `route_verification.py` pomáhají ověřit natočení
obrazem. Ani tyto moduly nemají dokonalé rozpoznání stejného místa.

## Paměť

`place_memory.py` ukládá omezený deník podle cíle hledání: pravděpodobný
název oblasti, orientační body, poznámky, návštěvy, co nebylo prohlédnuto
a zda byl cíl v pozorování vidět. Obrazové kotvy jsou soukromé. Do AI jde
omezený souhrn, nikoli neomezený přepis všech událostí. Pole
`room_fully_searched` zůstává nepravdivé: pár pohledů nepotvrzuje úplné
prohledání pokoje. Trvalé soubory žijí v `data/`, které Git ignoruje.

## Zdroje orientace v kódu

| Soubory | Obsah |
| --- | --- |
| `firmware/uno/` | Motorový protokol, ultrazvuk a limity. |
| `firmware/esp32cam/` | Kamera, provisioning, výkon, LAN, relay a diagnostika. |
| `server/app.py`, `relay.py` | API, autentizace, spojení s autem a provoz runnerů. |
| `server/static/` | Kokpit, hlasové ovládání a stránky simulátoru. |
| `server/ai_provider.py`, `*prompt.py` | Modelové adaptéry a prompty. |
| `server/place_memory.py`, `route_memory.py` | Paměť míst a cest. |
| `server/simulator*.py`, `sensor_navigator.py` | Simulátor a starší navigační experimenty. |
| `radar_service/` | Hloubková inference, rozhled, návrhy kroků a diagnostický web. |
| `tests/` | Jednotkové a integrační kontroly s testovacími adaptéry. |
