# Soukromé údaje a provoz

Veřejné jsou zdroje a obecná dokumentace. Soukromé musí zůstat:

- přístupové a API klíče, hesla, párovací JSON a skutečné secret hlavičky;
- Wi-Fi nastavení, osobní serverové adresy a certifikační soukromé klíče;
- snímky, zvuk, logy, mapy bytu, chatová historie a paměť míst;
- zálohy, firmware sestavený s klíči a obsah původní pracovní instalace.

`.gitignore` tyto běžné výstupy vylučuje. Nenahrazuje kontrolu obsahu:
tajemství vložené do běžného `.py` souboru by se jinak zveřejnilo.
Před každým pushem prohlédni `git diff --cached` a spusť Gitleaks:

```text
gitleaks dir . --redact
gitleaks git . --redact
```

Nikdy nepoužívej `git add -f` na tajné nebo datové soubory. Pokud klíč
unikne, nejprve jej zruš/vyměň u poskytovatele; smazání posledního commitu
neodstraní kopie a historii. Do GitHub issues nevkládej přístupy ani
neanonymizované logy.

Vzory mají neplatné domény a generátor vytváří vlastní hesla. Nevystavuj
instanci s demo heslem. Radarový proces naslouchá jen na localhostu a
veřejně jej zpřístupňuje přihlášená proxy kokpitu. Klíče ukládej s omezeným
přístupem; `chmod` v Pythonu není náhradou za správné Windows ACL.

Projekt řídí fyzické zařízení a je experimentální. Vyvíjí se s přítomným
operátorem a možností zastavit auto. Neprohlašuje spolehlivou detekci
schodů, osob ani všech překážek.
