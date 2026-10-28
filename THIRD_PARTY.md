# Externí součásti

Závislosti se instalují zvlášť, jejich licence zůstávají v platnosti.
Repozitář neobsahuje ZIP balíky výrobce, modelové váhy ani privátní Verity.

- KUONGSHUN AD176: hardware a původní podklady výrobce. Firmware projektu
  používá pinout a motorové směrové bajty odpovídající této stavebnici.
- [PlatformIO](https://platformio.org/), Arduino framework, Espressif
  framework, ArduinoJson a Servo: verze v `platformio.ini`.
- Python knihovny: seznamy `requirements.txt` a `requirements-simulator.txt`.
- [Depth Anything V2](https://github.com/DepthAnything/Depth-Anything-V2):
  model odhadu hloubky. Použit je Small; jiné velikosti mohou mít jiné
  licenční podmínky.
- [ONNX převod Small](https://huggingface.co/onnx-community/depth-anything-v2-small):
  modelová karta a zvlášť stahovaný `onnx/model.onnx`.
- Gemma a zvolený server inference se pořizují samostatně; dodržuj podmínky
  konkrétního modelu. Totéž platí pro OpenAI a hlasovou službu.

Licence pro vlastní kód Kvída zatím nebyla určena. Samotné zveřejnění na
GitHubu nepřiděluje projektu MIT/Apache licenci ani nemění licence
externích komponent.
