# Živý test: nalezení chromované psí misky — 28. 10. 2026

**Fyzické auto v závěrečné části testu rozpoznalo misku, přiblížilo se k ní
a samo ukončilo úkol.** Backend zaznamenal `completed`, `target_reached`,
`armed=false` a `motor_byte=0`. Finální soukromý snímek byl také vizuálně
zkontrolován: kovová miska je před autem.

Zadání: **„Najdi stříbrnou chromovanou psí misku na podlaze a zastav před ní.“**

Jde o první zde doložené dokončení tohoto úkolu, nikoli o prokázanou
spolehlivost navigace v libovolném bytě. Během předchozí části byla nutná
operátorská pauza a oprava. Celý večer tedy nebyl jednou nepřerušenou
autonomní jízdou.

## Co lze prohlédnout

- [Očištěný záznam jízdy](journey.json): všech **68 pozorování závěrečného
  úseku**, šest vybraných pozorování předchozího úseku a přehrání problematického
  snímku novým promptem. Obsahuje sonar, rozpoznání scény, plánované i hlášené
  provedené pohyby, ověření směru a stručné důvody rozhodnutí.
- [Ukázky promptů](prompt-examples.json): přesné šablony sestavené z pracovní
  verze pro vnímání, porovnání směru a výběr cesty. Jde o **offline
  rekonstrukci s uvedenými vstupy**, ne o zachycené HTTP požadavky z jízdy.
  Ukázka panoramatu používá ilustrační tři pohledy a prázdný deník.
- [Použité STT a TTS](../../HLAS.md): konkrétní enginy a cesta audia.

Původní fotografie interiéru zůstávají soukromé. Každý publikovaný řádek
obsahuje SHA-256 svého JPEG; při exportu byl porovnán se skutečným souborem.
Hash slouží ke spojení záznamů a rozlišení snímků. Bez původního obrázku
sám nedokazuje, co na něm bylo. Jde o záznamy projektu, nikoli nezávislou
certifikaci výsledku.

**Verze:** tento test běžel na novější pracovní instalaci než zdrojový
snapshot `cccf363` v tomto repozitáři. Tato publikace přidává dokumentaci
a důkazy; nesynchronizuje serverový kód ani firmware. Popsané opravy proto
nelze automaticky považovat za přítomné ve veřejných zdrojích.

## Co se opravovalo během testování

### Dorovnání směru podle skutečného obrazu

Pevné otočení o 300 ms bylo pro malé zbývající úhly příliš hrubé. Pracovní
verze nyní volí délku jednotlivé korekce z obrazově naměřeného rozdílu
a odhadu rychlosti otáčení, v rozsahu 60–300 ms. Zachová referenční snímek,
po korekci jej znovu porovná a nedovolí nekonečné dorovnávání bez pokroku.
Zkrácený impulz se zapíše i s délkou; nemá se vydávat za běžný 300ms krok
v odometrii ani kalibraci.

V úseku A, kroky 14–15, korekce **173 ms vlevo při PWM 130** snížila
obrazový rozdíl přibližně z **8,29° na 4,56°**. V závěrečném úseku B,
kroky 38–39, **280 ms vpravo při PWM 150** snížilo rozdíl z **25,64°
na 2,32°**. Jsou to obrazové odhady, nikoli měření enkodérem.

Krok B38 zároveň ukazuje, proč nestačí samotný slovní úsudek AI: model
odpověděl `match`, ale geometrie stále zjistila rozdíl 25,64°. Kód proto
nejdřív dorovnal směr; jízdu vpřed navrhl až po dalším snímku B39.

### Zavřené dveře nejsou průchod

V úseku A model označil zavřené dveře jako možný průchod. Operátor pozastavil
úkol na kroku 53, upravil prompt a po nasazení pokračoval ve stejném úkolu
novým radarovým úsekem B, jehož číslování začíná od jedné. Existuje také
pozdější pozorování A54; vybraný výřez A není úplný záznam všech událostí pauzy.

Prompt nově výslovně rozlišuje viditelnou podlahu **skrz otevřenou mezeru**
od dveří, panelu, rámu nebo proužku světla pod dveřmi. Při opětovném vyhodnocení
stejného uloženého snímku A38 se `passage_visible` změnilo na `false` a model
popsal zavřené dřevěné dveře. Tento samostatný pokus neovládal motory.
Krátká volná podlaha před dveřmi může stále dovolit krátký krok; neznamená,
že dveřmi lze projet.

## Závěrečný dojezd v úseku B

Od přijetí prvního do posledního snímku uplynulo **438,3 s**, tedy asi
7 minut 18 sekund. Bylo uloženo 68 různých JPEG hashů. Medián zaznamenané
doby části vyhodnocující aktuální obraz byl **1 988 ms**; nejde o celou
dobu kroku včetně kamery, radaru, otáčení a čekání.

| Krok | Co potvrzuje záznam | Další rozhodnutí |
| --- | --- | --- |
| 15–16 | Ověření zvoleného směru; další snímek hlásí vykonaných 7 impulzů vpřed. | Znovu vyhodnotit novou scénu, pak pokračovat. |
| 38–40 | Korekce směru 25,64° → 2,32°; po ní série 8 impulzů a nový snímek. | Další série vychází z nového pozorování. |
| 53 | Sonar 33 cm; model tvrdí `clear`, ale kontrola obrazové struktury přepíše stav na `unreadable`. | Žádná jízda vpřed; pokračuje otáčení při rozhledu. |
| 62–63 | Miska rozpoznaná s vysokou jistotou vlevo. | Natočit se k ní, poté znovu pozorovat. |
| 64 | Miska vpředu, kamerový odhad asi 94 cm. | 3 impulzy vpřed. |
| 65 | Před snímkem hlášeny 3 impulzy; nový odhad asi 77 cm. | 2 impulzy vpřed. |
| 66 | Před snímkem hlášeny 2 impulzy; nový odhad asi 61 cm. | 1 impulz vpřed. |
| 67 | Před snímkem hlášen 1 impulz; nový odhad asi 48 cm. | Stát a ověřit cíl ještě jedním snímkem. |
| 68 | Další odlišný snímek, miska stále rozpoznaná; žádný další pohyb. | `target_reached`, úkol dokončen. |

Finální odhad kamery byl **47 cm**, ultrazvuk u posledního pozorování
**88 cm**, v následném stavovém snímku **86 cm**. Čidlo mohlo vidět jinou
plochu než misku; přesná vzdálenost nebyla nezávisle změřena.

## Jak číst log a prompty

`proposed_actions` je plán po vyhodnocení snímku. `executed_before_frame`
jsou povely hlášené před následujícím snímkem. Nesmějí se zaměňovat:
například v úseku A bylo naplánováno sedm kroků, ale kvůli neplatnému
sonaru se provedly jen tři. Ani potvrzený motorový povel sám nedokazuje,
o kolik centimetrů se auto fyzicky posunulo.

Akce má tvar `[PWM, počet, směr]`. Běžný impulz trvá 300 ms; zkrácená
vykonaná korekce má navíc čtvrtý údaj s délkou, například
`[150, 1, "rotate_right", 280]`. Relativní čas snímku znamená jeho přijetí
přes HTTP, nikoli čas expozice ve firmwaru.

AI přes lokální vision endpoint (v kokpitu označený jako Gemma) dostává
aktuální obraz, čelní vzdálenost a popis cíle. Vnímání nemá historii zpráv;
výběr cesty z panoramatu má oddělený omezený deník míst. Porovnání směru
dostává dvě výslovně označené fotografie `REFERENCE` a `CURRENT`.
Přesnou identitu vah tento export nedokládá. OpenAI se při tomto běhu
nepoužívalo.

Výstup má JSON schema. Parametry šablon: vnímání `temperature=0.1`,
`max_tokens=450`; porovnání směru `0.1` / `180`; výběr cesty s deníkem
`0.3` / `480`. Všechny tři žádají `enable_thinking=false`.

`perception_after_guards` už obsahuje kontrolu odpovědi kódem; není to
syrová HTTP odpověď modelu. `planner_reason` je stručné odůvodnění aplikace,
někdy s vloženým textem modelu. Motorové série a jejich zkracování vybírá
také deterministický plánovač podle radaru a senzorů. Log tedy nezobrazuje
skryté uvažování modelu ani čistě jazykové řízení všech motorů.

## Co zůstává otevřené

- Úspěch jednoho závěrečného úseku nestačí pro měření spolehlivosti.
- B14 stále obsahuje nepodložené „Miska byla vidět v kuchyni“. Text paměti
  může zaměnit dřívější odhad za potvrzené pozorování. V exportu je chyba
  ponechána, nikoli redakčně opravena.
- V B63 neměla geometrie dost společných rysů; shodu směru tehdy posoudil
  model. Ne každé hlášení „směr ověřen“ znamená přesné geometrické měření.
- Prompt panoramatu stále popisuje přibližně rovnoměrné úhlové kroky.
  Skutečný pohyb může prokluzovat a úhly se mohou lišit.
- Mapa má drift, ultrazvuk výpadky a kamerová vzdálenost je odhad. Záznam
  neprokazuje přesný SLAM ani bezkolizní provoz ve všech situacích.

Pro změny dorovnávání prošlo 143 cílených kontrol; pro následnou změnu
promptu 46 kontrol, které se s předchozí sadou částečně překrývají.
Nejde o 189 unikátních testů ani o nové spuštění celé sady veřejného repozitáře.
Při samotné publikaci tohoto balíčku se auto znovu nerozjíždělo.
