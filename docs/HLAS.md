# Hlas: konkrétní TTS a STT

Ověřeno 28. 10. 2026 proti zapojení Kvída, kódu a konfiguraci služby Verity
a živým zdravotním endpointům hlasových služeb.

| Funkce | Co používá současná instalace | Kde se provádí |
| --- | --- | --- |
| TTS — text na řeč | **OmniVoice**, model `k2-fsa/OmniVoice`, hlas v režimu `clone`, jazyk `cs`. | Samostatná GPU služba, přes Verity. |
| STT — řeč na text | **Whisper large-v3-turbo** přes **faster-whisper / CTranslate2**, český přepis. | Samostatná služba na Windows s GPU. |
| Záznam a přehrávání | Web Audio API, `AudioWorklet`, vlastní detekce řeči, WAV. | Telefon nebo počítač s otevřeným kokpitem. |

ESP32 řeč nepřepisuje ani nesyntetizuje. Aktuální auto nepotřebuje přidaný
mikrofon nebo reproduktor: používá je zařízení s prohlížečem.
Volba Gemma/OpenAI pro rozhodování nemění tuto hlasovou cestu.

## TTS: OmniVoice

Používáme [OmniVoice od k2-fsa](https://github.com/k2-fsa/OmniVoice).
Nasazená služba skutečně importuje `OmniVoice` a načítá jeho lokální váhy;
její `/health` při kontrole hlásil `engine=omnivoice`, `status=ready`.
Referenční instalace běží na RTX 3090. To je popis nasazení, nikoli
tvrzení o minimálních hardwarových požadavcích.

Aktivní konfigurace Verity: `mode=clone`, `language=cs`, `num_step=32`,
`speed=1`, `seed=4242`. Existuje referenční WAV i jeho přepis.
Výsledný hlas se tedy odvozuje od reference; popis pro režim `design`
není v tomto režimu odesílán. Soukromá hlasová reference ani její přepis
nejsou součástí repozitáře.

Tok požadavku:

1. Kokpit odešle text na Kvído `POST /api/tts`.
2. Kvído předá nejvýše 600 znaků do Verity se stálou emocí `neutral`.
3. Verity sestaví parametry OmniVoice, připojí referenci a požádá
   hlasový backend o syntézu. Ten vrátí JSON s `audio_base64`.
4. Verity dekóduje WAV a kontroluje podezřelý šum, ticho či délku. Pro
   takový výstup má nejvýše tři pokusy s jiným seedem; současný kód však
   po třetím podezřelém výsledku poslední WAV stále vrací. Nejde tedy
   o záruku, že vadné audio nikdy neprojde.
5. Kvído vrátí `audio/wav`; prohlížeč jej přehraje přes Web Audio.

Fronta řeči v kokpitu omezuje čekající zprávy a při STOP nebo ztlumení
ruší přehrávání. Zdravotní endpoint nepotvrzuje, že uživatel zvuk skutečně
slyšel; záleží také na odemčení audia, hlasitosti a aktivním panelu.

## STT: faster-whisper large-v3-turbo

Backend používá [faster-whisper](https://github.com/SYSTRAN/faster-whisper),
tedy Whisper nad CTranslate2. Dokumentace běžící hlasové instalace uvádí
snapshot `mobiuslabsgmbh/faster-whisper-large-v3-turbo`; jeho původní
modelová stránka nyní přesměrovává na
[dropbox-dash/faster-whisper-large-v3-turbo](https://huggingface.co/dropbox-dash/faster-whisper-large-v3-turbo).

Živý `/health` potvrdil `ready=true`, `model=large-v3-turbo`,
`compute_type=int8_float32` a GTX 1660 SUPER. Dokumentace této instalace
uvádí faster-whisper 1.2.1, CTranslate2 4.8.2, češtinu a serverové
Silero VAD; verze balíčků ani parametry VAD se při této kontrole znovu
neověřovaly na Windows hostiteli. Nejde o cloudové OpenAI STT API.

Tok nahrávky:

1. `getUserMedia` získá mikrofon se souhlasem uživatele. `AudioWorklet`
   zachytává PCM při požadované vzorkovací frekvenci 16 kHz, mono.
2. `live-audio.js` měří šum, rozpozná řeč pomocí adaptivního prahu
   s hysterezí, zachová začátek promluvy a normalizuje hlasitost.
3. Každý odeslaný úsek je samostatně dekódovatelný 16bitový PCM WAV.
   Dlouhá souvislá řeč se dělí po 24 sekundách s překryvem 0,55 sekundy.
4. Prohlížeč volá `POST /api/stt?live=1`; konečný přepis přidá `final=1`.
   Kvído předá WAV Verity a ta jej pošle na chráněné `/transcribe`.
5. Verity očistí známé falešné přepisy ticha a vrátí JSON s `text`
   a případně `duration`. Frontend odstraní překrývající se slova mezi
   úseky a zobrazí přepis před odesláním jako zadání.

Browser drží nejvýše jeden aktivní přepis a při vybraných přechodných
chybách má nejvýše tři pokusy. Služba STT je sdílená; při obsazení může
vrátit 429. Klíč k ní zůstává na serveru, ne v JavaScriptu telefonu.

## Zapojení vlastní instalace

Kvído potřebuje adaptér kompatibilní s Verity. Jeho základní URL nastavuje
`ROVER_VERITY_URL`. Nenastavuj sem přímo libovolný OmniVoice/Whisper server:
jejich rozhraní se liší od rozhraní, které očekává Kvído.

| Rozhraní adaptéru | Vstup | Výstup |
| --- | --- | --- |
| `POST /api/tts` | JSON `{"text":"Ahoj","emotion":"neutral"}` | `audio/wav` |
| `POST /api/stt?live=1&final=1` | `audio/wav`, binární tělo | JSON `{"text":"Najdi misku","duration":2.8}` |
| `GET /api/status` | Bez těla | JSON obsahující booleany `llm`, `tts`, `stt` |

Repozitář obsahuje [proxy](../server/app.py),
[nahrávání a segmentaci](../server/static/live-audio.js),
[PCM worklet](../server/static/pcm-worklet.js) a
[kokpit s přehráváním](../server/static/app.js).
Celá Verity, její vlastní OmniVoice wrapper, váhy, hlasová reference
a interní přístupové údaje se instalují zvlášť a v tomto exportu nejsou.
Samotné `pip install -r requirements.txt` proto hlas nezprovozní.

Pro mikrofon na telefonu je potřeba zabezpečený kontext, typicky platné
HTTPS, a povolení prohlížeče. `localhost` je užitečný při lokálním vývoji;
prostá HTTP adresa jiného zařízení v LAN není ekvivalent.

## Co bylo ověřeno

Předchozí syntetický test přes veřejný panel prošel cestou TTS → WAV → STT:
TTS 1,19 s, WAV 3,27 s / 24 kHz / mono, STT 7,22 s. Přepis české věty
měl drobnou odchylku. To není měření reálného mikrofonu konkrétního telefonu.
Při psaní tohoto dokumentu byly ověřeny pouze konfigurace, kód a dostupnost
služeb; nový zvukový ani motorový test se nespouštěl.
