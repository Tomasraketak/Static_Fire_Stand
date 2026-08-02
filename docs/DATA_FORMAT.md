# Data format

## 1. Flash layout

```
0x10000000  +--------------------------+
            | sketch                   |
            +--------------------------+
            | (free space)             |
            +--------------------------+ <- _FS_start - log region size
            | journal A     4 kB       |
            | journal B     4 kB       |
            | slot 0       64 kB       |
            | slot 1       64 kB       |
            | ...                      |
            | slot 5       64 kB       |
            +--------------------------+ <- _FS_start
            | LittleFS                 |
            +--------------------------+
```

The region is derived at runtime from the linker symbols `_FS_start` and
`__flash_binary_end`. If it would overlap the sketch or the filesystem,
the firmware **disables logging** and reports it in `log_err` — it never
overwrites anything that belongs to something else.

A 64 kB slot is 256 pages: 1 header plus up to 255 data pages of 29
samples each = **7 395 samples**, roughly 92 s at 80 SPS.

## 2. Pages

Every page is 256 B, the program unit of NOR flash. Programming one takes
about 400 µs and is atomic as far as the record is concerned: either the
page is fully written and passes CRC, or it is discarded.

### Data page `SFP1`

| offset | type | field |
|---|---|---|
| 0 | u32 | magic `0x31504653` |
| 4 | u32 | burn_id |
| 8 | u16 | page_index |
| 10 | u16 | n_samples (max 29) |
| 12 | u32 | flags |
| 16 | u32 | crc32 |
| 20 | 29 × 8 B | samples |
| 252 | 4 B | padding |

`flags`: bit 0 `RESUME` (first page after an unplanned restart),
bit 1 `GAP` (a hole of unknown length precedes this page).

### Sample — 8 B

| offset | type | field |
|---|---|---|
| 0 | i32 | `t_us` — microseconds relative to T0, negative before ignition |
| 4 | u32 | `packed` |

`packed` = bits 0–23 the raw 24-bit HX711 value (two's complement),
bits 24–31 flags: 0x01 pyro energised, 0x02 continuity, 0x04 RBF key
inserted, 0x08 ADC saturated.

Conversion: `thrust [N] = (raw − tare_offset) / cal_counts_per_n`, both
taken from the header page.

### Header page `SFH1` (page 0 of a slot)

`magic`, `burn_id`, `crc32`, `boot_count`, `fw_version`,
`cal_counts_per_n` (float), `tare_offset` (i32), `preroll_ms`,
`recording_ms`, `ignition_ms`, `countdown_ms`, `t0_unix`, 4 × reserved.

Written **before** the pre-roll starts, so even a truncated burn carries
its calibration and can be evaluated.

### Footer page `SFF1`

`magic`, `burn_id`, `crc32`, `n_pages`, `n_samples`, `t_last_us`,
`t_pyro_on_us`, `t_pyro_off_us`, `resume_count`, `clean_finish`,
6 × reserved.

**A missing `SFF1` is a diagnosis:** the record was interrupted. The
analysis reports it and switches off the propellant mass calculation,
because the record does not end at rest.

### Journal `SFJ1`

`magic`, `seq`, `crc32`, `total_burns`, `active_slot`, `state`,
`boot_count`, `resume_count`, `cal_counts_per_n`, `tare_offset`,
`slot_erased_mask`, 5 × reserved.

Two sectors of 16 records each, written in sequence. When a sector fills:
the *other* sector is erased, a new record with a higher `seq` is written
there, and only then is the original erased. **At no point are both
sectors without a valid record.** At boot both are scanned and the
highest `seq` that passes CRC wins.

## 3. Serial transfer

Command `p` (or `p <slot>`):

```
#BEGIN SFDUMP v2
#INFO {"fw":"2.0.0","boots":7,...}
#SLOT 0 pages=137
QkVHSU4g...            <- base64 of one 256 B page, 344 characters
...
#ENDSLOT 0
#SLOT 1 pages=64
...
#ENDSLOT 1
#END SFDUMP
```

Base64 because USB CDC is a text channel and raw binary can collide with
flow control. One line per page, so a corrupted line costs exactly one
page and the rest of the burn survives.

The host (`sf_protocol.py`) verifies the CRC of every page, drops the bad
ones, sorts samples by time and removes duplicates. How much it dropped
shows up in the summary under measurement quality.
