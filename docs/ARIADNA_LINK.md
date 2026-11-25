# Ariadna link

The stand can be connected to an **Ariadna V2** flight computer over I2C. With the link on:

* the stand still runs the countdown, still records the thrust into its own power-fail safe
  log, and is still operated by the physical button or the WiFi page, exactly as before;
* the stand does **not** fire its own igniter. At T0 it tells Ariadna to fire **its pyro 1**;
* after the burn, Ariadna copies the stand's whole record over I2C into its own flash and merges
  it with what it measured itself (battery voltage, current, clamp position).

The Ariadna side is `Ariadna_V2/Examples/Static_Fire_Link` in
[Daidalos_Landing_Rocket_Code](https://github.com/Tomasraketak/Daidalos_Landing_Rocket_Code);
this page is the stand's half and the protocol between the two.

**Without the link nothing changes.** The mode is a setting stored on the stand and it ships as
*standalone*: the stand fires its own MOSFET, never touches GP4 / GP5 and behaves as it always did.

## 1. Switching it on

```
serial console:   link ariadna        (or: link off)
                  reboot
tools:            python static_fire.py --link ariadna        (restarts the stand for you)
```

`link` alone shows the mode and what the stand thinks of Ariadna. The setting survives power cuts
(it lives in the journal). In `ariadna` mode the web page grows an **Ariadna** row
(`READY` / `NOT READY` / `NO LINK`), an **Ariadna pyro 1** row (`safe (disarmed)` / `ARMED` / `FIRED`, `unknown` without a link), and the Continuity row shows Ariadna's igniter (`OK (Ariadna)` / `OPEN (Ariadna)` / `no link to Ariadna`). The START COUNTDOWN button stays disabled until Ariadna is ready.

> In `ariadna` mode **the gate on GP21 is never driven**. Do not connect an igniter to this board
> and to Ariadna at the same time.

## 2. Wiring

| Stand (Pico 2 W) | | Ariadna V2 Master |
|---|---|---|
| GP4 (I2C0 SDA) | ↔ | `I2C1` or `I2C2` uSup, **SDA** (GP8) |
| GP5 (I2C0 SCL) | ↔ | `I2C1` or `I2C2` uSup, **SCL** (GP9) |
| GND | ↔ | uSup **GND** |

* **Leave the 3V3 pins unconnected.** Two separately powered boards must not have their 3V3 rails tied.
* The stand has **no pull-ups**; Ariadna's bus already has 10 kΩ to 3V3 and runs at 400 kHz, which is
  right for a short cable. If a long one gives bus errors, add 4.7 kΩ pull-ups to Ariadna's 3V3 at
  Ariadna's end rather than anything on the stand.
* The stand is slave **0x2C** on the same bus as Ariadna's PCA9685 (0x40). Keep the cable short and
  out of the igniter wiring: this bus also carries the servo driver.
* The igniter goes on **Ariadna pyro 1**; the stand's own ignition output stays unused.
* Power the two boards from their own batteries; the shared GND is the only thing between them.

## 3. What happens in a run

```
 stand                                      Ariadna
 ─────                                      ───────
 idle, RBF key out, load cell + log OK      idle, RBF pulled, igniter present,
 READY ◄───────────────────── both say it ─────────► READY            (the handshake)

 button / WiFi: START COUNTDOWN
   countdown 15 s (+ the stall dodge),      sees COUNTDOWN with a run number,
   pre-roll recording from T-3 s            arms pyro 1 on the Navigation MCU,
                                            logs from T-3.5 s
   a loss of Ariadna / its READY here
   aborts the countdown ("AB_LINK")
 T0: raises IGNITE, state = RECORDING ───►  sees IGNITE for the run it watched,
                                            fires pyro 1 for the stand's igniter on-time
                                            and acks the run (its heartbeat)
                                            thrust > 100 N: clamp 50 % / minimum / 75 % / open
 recording window ends, record closed
 DATA_READY ◄──────────────────────────────  copies the pages (64 B per read, CRC32 per
                                            page, retried), merges, writes its flash,
 data_pending cleared ◄────────────────────  tells the stand "data copied"
 READY again
```

The stand ends the burn **on its own clock** (the recording window), whatever Ariadna does; Ariadna
adapts to it. Nothing Ariadna does can stop the stand from recording.

### Why it cannot fire by accident

* Ariadna fires only for a countdown it **watched from the inside**: it saw the stand in
  `COUNTDOWN` with a run number, armed, and then saw `IGNITE` for that same run number. An IGNITE
  from nowhere, a stale one or one for another run does nothing.
* IGNITE older than 150 ms (measured on the stand's own clock) is refused.
* IGNITE drops after 500 ms if not acknowledged, so a late Ariadna cannot fire "after the fact".
* Run numbers never repeat the ones Ariadna currently holds, and a stand that restarts cannot have a
  stale acknowledgement swallow a new run.
* Every frame carries a CRC-8 and a magic byte; a bus that answers `0xFF` or a flipped bit is
  rejected (the STATUS frame is tested for every single-bit flip).
* The Navigation MCU's own rules still apply: armed only 60 s, heartbeat from the Master, one pulse
  of 20–1000 ms, every pulse ends disarmed.
* The stand still checks its **own RBF key** at T0; with the key in, nothing is raised.

### Time

Ariadna has no shared clock with the stand. Every STATUS says how long ago T0 was, or how long until
it, **at the moment the stand answered** (`t_rel_us`, negative before T0). Ariadna puts that on its
own clock, so its samples land on the stand's time axis to within about a millisecond before T0 and a few
hundred microseconds after it (the stand re-states T0 exactly once it has happened). T0 is
defined as before: the instant the stand commanded the ignition (it is what the stand's own record
and analysis call t = 0).

## 4. Protocol

Canonical definition: [`firmware/StaticFire_Stand/sf_link_proto.h`](../firmware/StaticFire_Stand/sf_link_proto.h)
(no Arduino dependency; Ariadna carries a byte-for-byte copy, and `make sflink-sync` on its side checks it).
Version `SFL_PROTO_VERSION = 1`; each side refuses the other's other versions.

Ariadna is the I2C master; the stand is the slave at **0x2C**. Everything the stand says is a *level*
in a register that Ariadna polls, everything Ariadna says is a level in a heartbeat it writes about
20×/s, so a lost frame is repeated by the next and nothing has to be retransmitted.

| register | direction | size | content |
|---|---|---|---|
| `0x00` STATUS | read | 16 B | magic `0x5F`, state, flags, run_seq, thrust (mN), `t_rel_us`, pages, abort reason, CRC-8 |
| `0x01` INFO | read | 16 B | protocol, firmware, burn id, igniter on-time, pre-roll, recording window, CRC-8 |
| `0x10` HEARTBEAT | write | 8 B | magic `0xA7`, flags (READY, ARMED, FIRED, CONT, BUSY, FAULT), `ack_seq`, `data_seq`, CRC-8 |
| `0x20` PAGE | write 5 B, then read 66 B | | request `page, chunk`; reply `[code][64 data][CRC-8]` |

STATUS flags: `READY` (idle, load cell + log OK, RBF key out, nothing pending) · `RBF_OUT` · `IGNITE`
(latched at T0 until acknowledged) · `DATA_READY` (a finished burn waits to be copied) · `LINK_OK` ·
`ARIADNA_RDY` · `HX_OK` · `LOG_OK`.

**PAGE** serves the last burn's pages byte for byte in the on-flash format
([DATA_FORMAT.md](DATA_FORMAT.md): `SFH1` header, `SFP1` data, `SFF1` footer), 4 chunks of 64 B per page.
A copy therefore decodes with the same Python tools as a dump from the stand. Pages are refused
(`code = BUSY`) unless the stand is idle, and while a copy is pending the idle erase-ahead pauses, so
neither a flash program nor an erase can run under a read.

The I2C slave runs in interrupt context on **core 1**, the same core as WiFi; core 0 (sampling, ignition
timing, recording) is never involved in a transaction. The time-critical sampling loop is unchanged.

## 5. If it does not work

| symptom | look at |
|---|---|
| web page says `NO LINK` | wiring (SDA/SCL swapped is the usual one), common GND, pull-ups, that Ariadna runs `Static_Fire_Link`; `link` on the console |
| `NOT READY` | Ariadna's console says why (`s`): RBF pin in, no igniter on pyro 1, Navigation MCU not running `Pyro_Companion`, flash slot still erasing |
| countdown aborts with `Ariadna not ready / link lost` | Ariadna dropped out or withdrew READY during the countdown: working as intended |
| the burn is recorded but nothing ignited | Ariadna's console: did it see the countdown, arm, and say `FIRE`? Is the pyro 1 pulse (the stand's igniter on-time, at most 1000 ms) long enough for your igniter? |
| `START COUNTDOWN` greyed out after a run | Ariadna has not finished copying the record; `l` on Ariadna shows what is stored. `link off` + `reboot` is the way out if Ariadna is gone |

## 6. Tests

```
cd tests && make          # frames, handshake, ack levels, page codes, abort rules (host, no board)
cd tests && make syntax   # the firmware against a do-nothing Arduino shim
```

The Ariadna repository's `make sflink-loop` plays whole runs between this code and Ariadna's supervisor
with the read landing at every 100 µs offset around T0.
