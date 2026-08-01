# Static Fire Stand

Zkušební stend pro statické zážehy raketových motorů na tuhé palivo.
Řídicí jednotka je **Raspberry Pi Pico 2 W**, tah se měří tenzometrem
přes **HX711**, zápal obstarává MOSFET spínaný z pinu 21.

Repozitář obsahuje dvě části:

| Adresář | Co to je |
|---|---|
| `firmware/StaticFire_Stand/` | firmware pro Pico 2 W (Arduino / arduino-pico) |
| `tools/` | stahování dat z stendu, analýza, grafy, export do Excelu |

---

## 1. Co se změnilo oproti první verzi

### Záznam dat odolný proti výpadku napájení

Původní verze psala CSV přes `LittleFS` a `f.printf()`. To má dvě vady,
kvůli kterým se dá o zážeh přijít:

* text se hromadí v RAM bufferu a na flash se dostane až při `f.close()`
  – **reset uprostřed hoření = prázdný soubor**,
* zápis do souborového systému uprostřed hoření může při výpadku
  poškodit adresář a vzít s sebou i starší zážehy.

Nová verze nepoužívá při hoření souborový systém vůbec:

* data jdou do **vyhrazené oblasti syrové flash** hned pod oblastí LittleFS,
* každá **256bajtová stránka** má vlastní magii, index, počet vzorků a **CRC32**,
* slot pro další zážeh se **maže dopředu**, když stend stojí; při hoření
  se tedy jen programují stránky (~400 µs), nikdy se nemaže sektor (~50 ms),
* stav (který slot je živý, kolik zážehů proběhlo, kalibrace) drží
  **A/B žurnál** ve dvou sektorech; při rotaci je vždy aspoň jeden platný.

**Nejhorší možná ztráta při výpadku je jedna stránka, tedy 29 vzorků
(≈ 360 ms při 80 SPS).** Všechno starší už na flash bezpečně leží.

### Navázání záznamu po resetu

Když žurnál po startu říká, že zážeh běžel, firmware:

1. **nechá pyro kanál vypnutý** – nevíme, jestli motor už nehoří,
   a znovu pouštět proud do palníku hořícího motoru se nedělá,
2. dohledá poslední neporušenou stránku ve slotu,
3. **naváže záznam** ve zbytku okna a označí první stránku příznakem
   „mezera neznámé délky“,
4. analytický skript to pak vypíše jako upozornění, ne jako tichou chybu.

### Bezpečnost

* `PIN_IGNITION` se sráží na LOW jako **úplně první** operace v `setup()`,
  ještě před `pinMode()`, aby pin při startu neproblikl.
* Jediná funkce `pyroSet()` smí sepnout gate a sama znovu kontroluje
  stav, RBF klíč i časové okno – nezávisle na tom, co si myslí volající.
* **Nezávislý tvrdý limit** `IGNITION_MAX_MS`: i kdyby se hlavní podmínka
  přeskočila, gate nemůže zůstat sepnutý déle.
* Hlídací pes (watchdog) 4 s.
* Přerušení odpočtu: zasunutí RBF, tlačítko, tlačítko ABORT ve webu,
  ztráta kontinuity palníku, výpadek tenzometru.
* Fyzické tlačítko musí být **podrženo** 750 ms, jedno ťuknutí nestačí.
* Webový kód se porovnává v konstantním čase a po 5 chybách je minuta blokace.
* Odpočet se nedá spustit bez kontinuity palníku (`REQUIRE_CONTINUITY_TO_ARM`).

> **Hardware, který si musíš ohlídat sám:** gate MOSFETu potřebuje
> **pulldown 10 kΩ na GND**. Při resetu se GPIO přepne na vstup a bez
> pulldownu zůstane gate plovoucí – to je jediný stav, který firmware
> ošetřit nedokáže.

### Měření

* Vlastní neblokující ovladač HX711 místo knihovny: hlásí poruchu
  senzoru, umožňuje časovat vzorek s přesností na desítky µs a nikdy
  se nezasekne, když čip přestane odpovídat.
* Ukládá se **surová hodnota ADC**, ne přepočtený newton. Kalibrace
  i tára jsou v hlavičce každého zážehu, takže se dají **přepočítat
  zpětně**, když se ukáže, že kalibrace byla mimo.
* **3 s předzáznamu** před povelem (dřív 5 vzorků, tj. ~50 ms).
  Z toho se počítá klidová hodnota a šum – bez nich není odkud brát
  práh detekce zapálení ani hmotnost paliva.
* Okno záznamu 20 s (dřív 5 s). Reálné měření ukázalo zpoždění
  zapálení 1,7 s, takže 5 s bylo těsně.

### Web

Živý graf tahu, odpočet, stav skladu, tlačítko ABORT, TARE, hlášení
příčiny posledního přerušení. Jádro 1 (web) **nikdy nesahá na flash
ani na pyro** – umí jen vznést požadavek, který jádro 0 ověří proti
fyzickým pojistkám.

---

## 2. Zapojení

| Pin Pico | Funkce | Poznámka |
|---|---|---|
| GP2 | HX711 DT | |
| GP3 | HX711 SCK | |
| GP6 | tlačítko FIRE | k 3V3, `INPUT_PULLDOWN` |
| GP11 | RBF klíč | HIGH = zasunuto = bezpečno |
| GP21 | gate MOSFETu | **nutný pulldown 10 kΩ na GND** |
| GP22 | SK6812 / WS2812 | stavová dioda |
| GP28 | kontinuita palníku | HIGH = palník připojen |

Napájení 2S LiPo. Pyro okruh měj galvanicky oddělený od logiky nebo
aspoň s vlastním předřadným odporem – proud palníkem nesmí procházet
zemí měřicí části, jinak se to projeví na křivce tahu.

**RATE pin HX711 spoj na VCC**, jinak čip běží na 10 SPS a z křivky
tahu zbude pár bodů.

---

## 3. Firmware

Nahraj přes Arduino IDE s [arduino-pico](https://github.com/earlephilhower/arduino-pico)
(deska *Raspberry Pi Pico 2 W*). Potřebné knihovny: `Adafruit NeoPixel`.
`WiFi`, `WebServer`, `LittleFS` jsou součástí jádra.

Ve `Flash Size` nech nějaký prostor pro souborový systém, nebo klidně
0 – log si bere prostor pod oblastí LittleFS a při kolizi se sám vypne
a ohlásí to (`log_err` v `i`).

Konfigurace je celá v `firmware/StaticFire_Stand/config.h`:
časování sekvence, piny, SSID, heslo, kód pro odpal, počet slotů.

**Změň `AP_PASSWORD` a `SECRET_CODE` před prvním ostrým zážehem.**

### Sériové příkazy (115200 Bd)

```
?           nápověda
i           stav zařízení jako JSON
t           vynulování tenzometru
cal <N>     kalibrace známou silou v newtonech
calg <g>    kalibrace známým závažím v gramech
l           výpis slotů
p           výpis všech zážehů (base64 stránky)
p <slot>    výpis jednoho slotu
d           smazání všech dat
abort       přerušení odpočtu
s           jedno měření tahu
```

---

## 4. Vyhodnocení dat

```bash
pip install -r tools/requirements.txt
```

```bash
# stáhne vše ze stendu, vyhodnotí, uloží grafy + CSV + XLSX
python tools/static_fire.py

# konkrétní port
python tools/static_fire.py --port COM5

# známá navážka paliva – Isp pak sedí, nespoléhá se na posun váhy
python tools/static_fire.py --palivo 42.5

# offline vyhodnocení dřív staženého výpisu
python tools/static_fire.py --ze-souboru vysledky/20260716_135131/vypis_*.txt

# stará data z firmwaru V0
python tools/static_fire.py --stare-csv zazeh_1_*.csv

# správa stendu
python tools/static_fire.py --info
python tools/static_fire.py --tara
python tools/static_fire.py --kalibrace 500     # závaží 500 g
python tools/static_fire.py --smazat
```

Každé stažení nejdřív **uloží syrový výpis** do souboru a teprve pak
ho zpracuje. Když se analýza něčím zadrhne, data už jsou v bezpečí
na disku a dají se pustit znovu přes `--ze-souboru`.

### Co skript spočítá

**Časování zapálení** (od povelu, tedy od sepnutí pyro kanálu):

* zpoždění do prvního pohybu – práh je `max(6σ šumu, 1 % špičky)`
  a musí ho překročit tři vzorky po sobě, aby jeden zákmit neposunul výsledek
* časy dosažení 5 / 10 / 25 / 50 / 75 / 90 / 95 / 100 % špičky (i pro doběh)
* náběh 10 → 90 %, maximální strmost náběhu v N/s
* kdy se rozpojil pyro kanál

**Křivka tahu:**

* špičkový tah a jeho čas, poměr špička/průměr
* doba hoření (T0 → pokles pod 5 %) i action time (5 % → 5 %)
* průměrný tah počítaný z action time i od T0
* těžiště křivky a čas, kdy motor odevzdal polovinu impulsu

**Impuls a účinnost:**

* celkový impuls lichoběžníkovou integrací
* **korekce úbytku hmoty**: jak palivo ubývá, klesá i klidová hodnota
  váhy. Posun se interpoluje váženě podle už spáleného impulsu, ne
  lineárně v čase – palivo neubývá rovnoměrně
* hmotnost paliva z posunu klidové hodnoty, nebo zadaná přes `--palivo`
* Isp, efektivní výtoková rychlost
* třída motoru podle NAR/TRA a označení typu `H169`

**Kvalita měření** – vzorkovací frekvence, mezery, šum a drift baseline,
saturace ADC, zahozené stránky s vadným CRC, chybějící závěrečná stránka,
navázání po restartu. Když něco nesedí, skript to napíše místo toho,
aby vydal hezké, ale nesmyslné číslo.

### Výstupy

```
vysledky/20260716_135131/
├── vypis_20260716_135131.txt      syrový výpis ze stendu (záloha)
├── zazeh_001_data.csv             surová data, český Excel
├── zazeh_001_data.xlsx            list Souhrn + Data + Náběh, s grafem
├── zazeh_001_souhrn.csv           jen souhrnná tabulka
├── zazeh_001_grafy.png            5 grafů na jednom listu
├── prehled_zazehu.csv             řádek na zážeh, na porovnání šarží
└── porovnani_zazehu.png           křivky všech zážehů přes sebe
```

CSV mají oddělovač `;`, desetinnou **čárku**, BOM a na prvním řádku
`sep=;`. Otevřou se dvojklikem v českém Excelu správně, bez importního
průvodce a bez toho, aby si Excel spletl čísla s datem.

XLSX ukládá čísla jako čísla, takže se zobrazí podle lokálního
nastavení automaticky, a obsahuje nativní Excelový graf.

---

## 5. Poznámky z reálného měření

Kontrolní vyhodnocení zážehu z 16. 7. 2026 (starý firmware, import
přes `--stare-csv`) ukázalo tři věci, které stojí za zapamatování:

* **Motor se zapálil 1,715 s po povelu** – tedy 718 ms poté, co
  firmware už odpojil palník. Zápalná směs dohořívala sama. Proto je
  v nové verzi okno záznamu 20 s, ne 5 s.
* **Klidová hodnota před zážehem 0,23 N, po zážehu −2,26 N.** Váha
  přešla do záporných čísel, takže stend po zážehu nesedí stejně jako
  před ním. Než budeš věřit hmotnosti paliva dopočtené z tohoto posunu,
  ověř ji navážkou a předej ji přes `--palivo`.
* **Mezera 107 ms těsně u T0** je od `while (millis() < planned_ignition_time)`
  ve staré verzi. Nová verze v tom okně normálně vzorkuje.

---

## 6. Bezpečnostní minimum

Tohle je zařízení, které schválně zapaluje raketový motor.

1. RBF klíč zasunutý vždy, když je někdo blíž než na bezpečnou vzdálenost.
2. Palník se připojuje jako **poslední** krok, po odchodu od stendu.
3. Kontinuita se kontroluje z bezpečné vzdálenosti přes web, ne u stendu.
4. Po nezahoření **počkej aspoň 60 s**, teprve pak k stendu jdi, a jako
   první zasuň RBF.
5. Motor miř do prostoru, kde výtoku nic nestojí v cestě, a počítej
   s tím, že se komora může roztrhnout.
6. Mimo dosah dětí, hasicí přístroj po ruce, a nikdy sám.

Software může jen zabránit tomu, aby proud tekl do palníku v nesprávný
okamžik. Zbytek je na tobě.
