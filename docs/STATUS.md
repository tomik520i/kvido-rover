# Stav projektu — 28. října 2026

Tento dokument rozlišuje implementaci od prokázaných výsledků.

## Nový výsledek: večerní test 28. října

Novější pracovní instalace dokončila hledání chromované psí misky:
závěrečný úsek má 68 pozorování a končí `target_reached`, `completed`,
`armed=false` a motory na nule. Předcházel mu úsek s operátorskou
pauzou na opravu promptu. [Podrobný záznam, limity a ukázky promptů](evidence/2026-10-28-bowl/README.md)
jsou publikované bez fotografií interiéru a soukromých přístupů.

Přibylo také ověřené [zapojení hlasu: OmniVoice TTS a faster-whisper STT](HLAS.md).
Tato aktualizace je dokumentační. Zdrojový snapshot `cccf363` nebyl
synchronizován s nasazenými opravami dorovnávání směru a vnímání.

**Následující soupis popisuje původní veřejný snapshot**, připravený
z podkladů do 26. října a zveřejněný 28. října. Jeho starší výsledky
a výsledky testovací sady se nesmějí zaměňovat s novým živým testem.

## Co máme

- Vlastní firmware UNO a ESP32-CAM, pohybové povely, náklon a světlo.
- Wi-Fi provisioning přes AP auta, lokální API a odchozí WSS relay.
- Mobilní kokpit s kamerou, ovládáním, textem, nastavením AI a hlasovými prvky.
- Serverový `TaskRunner` a novější `AlphaRunner`; průběh není pouze animace
  v prohlížeči. Běh, stav a obnovení spojení spravuje backend.
- Snímky před/po pohybu, telemetrie, log rozhodnutí a stručný důvod akce.
- Rozhled více směry, uchování snímků vybraného směru a obrazové ověření
  natočení před jízdou.
- Kamerový odhad volné podlahy, lokální occupancy mapa, odhad pohybu a
  konzervativní lokální zarovnání skenů.
- Paměť pravděpodobných míst: popisy, orientační body, co bylo vidět,
  nezpozorovaný cíl a dosud neprohlédnuté směry.
- Simulátor a automatické testy velké části rozhodování, transportu a UI.
- Diagnostika ESP, oddělování síťových úloh a adaptivní FPS/rozlišení kamery.

## Historické živé pokusy před novým večerním testem

Auto provádělo motorové impulzy a panoramatický rozhled. Při jednom běhu
nasbíralo 13 různých snímků rozhledu; ověření směru vyžádalo další natočení
a po shodě dovolilo jízdu. Paměť pravděpodobné kuchyně se načetla i po
restartu serverové části. To jsou dílčí výsledky, nikoli dokončené hledání.

**Chromovaná psí miska nebyla v zaznamenaném závěrečném testu spolehlivě
nalezena.** V několika pokusech se zhoršovalo spojení, mapa nebo rozhodnutí
o směru. Poslední test operátor ukončil.

## Známé nedostatky

1. **Pět pokusů má mít užší význam.** Současný
   `radar_service.acquire_observation` opakuje získání pozorování také při
   nízkém překryvu mapy a dalších nejasnostech. Požadavek je až pět pokusů
   pouze při neúspěšném získání snímku; nejde o pět celých radarových
   rozhlédnutí. Tato oprava v tomto snapshotu ještě není hotová.
2. **Mapa není spolehlivý SLAM.** Motorový čas je jen odhad pohybu. Prokluz,
   slabé obrazové rysy a opakující se stěny vedou k driftu. Některé skeny se
   správně odmítnou, ale mapa pak zůstává děravá. Celá otočka není vždy
   obrazově uzavřená a ověřená.
3. **Kamerová hloubka je odhad.** Kalibrace byla měřena na konkrétním kuse
   auta, povrchu a několika scénách. Ultrazvuk může mířit jinam než kamera;
   absence odrazu neprokazuje volnou cestu.
4. **Historie místa není důkaz prohledání místnosti.** „Cíl nebyl v těchto
   záběrech“ nesmí znamenat „v místnosti určitě není“. Identita míst se
   při podobných scénách může splést.
5. **Výpadky se stále musí dlouhodobě ověřit.** Retry a diagnostika existují,
   ale stabilita závisí také na napájení, Wi-Fi a zatížení kamery.
6. **Více generací řízení.** Starší TaskRunner, senzorový simulátor a
   kamerová alfa mají různé plánovače. Ne každé nastavení kokpitu se
   vztahuje na všechny. Následující práce má rozhraní sjednotit.
7. **Hlas vyžaduje externí službu.** Verity není součástí tohoto repozitáře;
   přiložen je klient/proxy rozhraní, nikoli přístupy nebo celý hlasový stack.

## Další postup

1. Zúžit pět pokusů na selhání snímku a doplnit test jejich přesného významu.
2. Testovat znovunavázání spojení odděleně od rozjezdu; potvrdit nový obraz
   a telemetrii, nepřehrávat staré motorové povely.
3. Porovnat plánovaný, obrazově ověřený a skutečně vykonaný směr v logu.
4. Ověřit mapování ve změněném simulovaném bytě a potom v krátkém živém testu.
5. Až potom hodnotit celý úkol hledání misky včetně návratu z chybné cesty.

## Ověření veřejné kopie

Kontrola 28. 10. 2026 na Windows / Python 3.11:

- `python -m pytest tests -q`: **1374 prošlo, 13 přeskočeno**. Volitelné
  záznamy a soukromá upstream kopie hlasového kódu nejsou distribuovány.
  Zůstávají varování knihoven o budoucím odstranění některých rozhraní.
- Sestavení PlatformIO: **UNO a ESP32-CAM UART0 úspěšné**, s lokálně
  dostupnou cache závislostí a novými testovacími klíči; relay vypnutý.
- HTTP smoke test simulátoru: start, přihlášení, režim simulace a JPEG
  kamery v pořádku; **0 AI volání, 0 fyzických povelů**.
- Gitleaks nad připravenými soubory: bez nálezu. Další lokální kontrola
  porovnala obsah s dostupnými skutečnými klíči a hledala původní osobní
  serverové adresy/cesty; bez nálezu.

Dvě staré testovací náhrady kamery byly upraveny pro parametr `preview`.
Test proti neveřejné upstream kopii hlasu se bez ní výslovně přeskočí,
nepředstírá její úspěšné porovnání. Soukromé záznamy a snímky nejsou
součástí repozitáře. V této publikační úloze se neflashoval firmware ani
neovládaly fyzické motory.
