# Formát dat

## 1. Rozložení flash

```
0x10000000  ┌──────────────────────────┐
            │ sketch                   │
            ├──────────────────────────┤
            │ (volné místo)            │
            ├──────────────────────────┤ ← _FS_start − velikost logu
            │ žurnál A     4 kB        │
            │ žurnál B     4 kB        │
            │ slot 0      64 kB        │
            │ slot 1      64 kB        │
            │ …                        │
            │ slot 5      64 kB        │
            ├──────────────────────────┤ ← _FS_start
            │ LittleFS                 │
            └──────────────────────────┘
```

Oblast se odvozuje za běhu z linkerových symbolů `_FS_start` a
`__flash_binary_end`. Kdyby se překrývala se sketchem nebo se
souborovým systémem, firmware **logování vypne** a nahlásí to
v `log_err` – nikdy nepřepíše cizí data.

Slot 64 kB = 256 stránek = 1 hlavička + až 255 datových stránek
× 29 vzorků = **7 395 vzorků**, tedy asi 92 s při 80 SPS.

## 2. Stránky

Všechny stránky mají 256 B, což je programovací jednotka NOR flash.
Zápis jedné stránky trvá ~400 µs a je nedělitelný z pohledu záznamu:
buď je stránka celá zapsaná a projde CRC, nebo se zahodí.

### Datová stránka `SFP1`

| offset | typ | pole |
|---|---|---|
| 0 | u32 | magic `0x31504653` |
| 4 | u32 | burn_id |
| 8 | u16 | page_index |
| 10 | u16 | n_samples (≤ 29) |
| 12 | u32 | flags |
| 16 | u32 | crc32 |
| 20 | 29 × 8 B | vzorky |
| 252 | 4 B | výplň |

`flags`: bit 0 `RESUME` (první stránka po neplánovaném restartu),
bit 1 `GAP` (před stránkou je díra neznámé délky).

### Vzorek – 8 B

| offset | typ | pole |
|---|---|---|
| 0 | i32 | `t_us` – mikrosekundy vůči T0, před zážehem záporné |
| 4 | u32 | `packed` |

`packed` = bity 0–23 surová 24bitová hodnota HX711 (dvojkový doplněk),
bity 24–31 příznaky: 0x01 pyro sepnuto, 0x02 kontinuita, 0x04 RBF
zasunutý, 0x08 saturace ADC.

Přepočet: `tah [N] = (raw − tare_offset) / cal_counts_per_n`,
obojí z hlavičkové stránky.

### Hlavičková stránka `SFH1` (stránka 0 slotu)

`magic`, `burn_id`, `crc32`, `boot_count`, `fw_version`,
`cal_counts_per_n` (float), `tare_offset` (i32), `preroll_ms`,
`recording_ms`, `ignition_ms`, `countdown_ms`, `t0_unix`, 4× rezerva.

Zapisuje se **před** začátkem předzáznamu, takže i useknutý zážeh má
kalibraci a jde vyhodnotit.

### Závěrečná stránka `SFF1`

`magic`, `burn_id`, `crc32`, `n_pages`, `n_samples`, `t_last_us`,
`t_pyro_on_us`, `t_pyro_off_us`, `resume_count`, `clean_finish`,
6× rezerva.

**Chybějící `SFF1` je diagnóza:** záznam byl přerušen. Analýza to
napíše jako upozornění a vypne dopočet hmotnosti paliva, protože
záznam nekončí v klidovém stavu.

### Žurnál `SFJ1`

`magic`, `seq`, `crc32`, `total_burns`, `active_slot`, `state`,
`boot_count`, `resume_count`, `cal_counts_per_n`, `tare_offset`,
`slot_erased_mask`, 5× rezerva.

Dva sektory po 16 záznamech, zapisují se za sebou. Když se sektor
naplní: smaže se ten druhý, zapíše se do něj nový záznam s vyšším
`seq`, a teprve pak se smaže původní. **V žádném okamžiku nejsou oba
sektory bez platného záznamu.** Při startu se prohledají oba a bere
se ten s nejvyšším `seq`, který projde CRC.

## 3. Přenos po sériové lince

Příkaz `p` (nebo `p <slot>`):

```
#BEGIN SFDUMP v2
#INFO {"fw":"2.0.0","boots":7,…}
#SLOT 0 pages=137
QkVHSU4g…            ← base64 jedné 256B stránky, 344 znaků
…
#ENDSLOT 0
#SLOT 1 pages=64
…
#ENDSLOT 1
#END SFDUMP
```

Base64 proto, že USB CDC je textový kanál a syrové binární bajty by
si mohly rozumět s řízením toku. Jeden řádek = jedna stránka, takže
poškozený řádek shodí přesně jednu stránku a zbytek zážehu zůstane.

Host (`sf_protocol.py`) ověří CRC každé stránky, poškozené zahodí,
vzorky seřadí podle času a odstraní duplicity. Kolik toho zahodil,
najdeš v souhrnu pod „kvalita měření“.
