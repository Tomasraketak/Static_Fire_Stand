# Static Fire Stand

Test stand for static firing of solid rocket motors. The controller is a
**Raspberry Pi Pico 2 W**, thrust is measured with a load cell through an
**HX711**, and ignition is switched by a MOSFET on GP21.

| Folder | What it is |
|---|---|
| `firmware/StaticFire_Stand/` | firmware for the Pico 2 W (Arduino / arduino-pico) |
| `tools/` | download, analysis, charts, Excel export — GUI (`sf_gui.py`) and console (`static_fire.py`) front ends |
| `docs/` | on-flash and on-wire data formats |

If you just want to fire a motor, jump to
[**4. Operating tutorial**](#4-operating-tutorial). If you want to know
why the recorder is built the way it is, start at section 1.

---

## 1. What changed from the first version

### Recording that survives a power cut

The original wrote CSV through `LittleFS` and `f.printf()`. Two things
about that will lose you a burn:

* the text sits in a RAM buffer until `f.close()`, so **a reset mid-burn
  leaves an empty file**,
* writing to a filesystem during the burn opens a window where a power
  cut can damage the directory and take older burns with it.

The new firmware does not touch the filesystem during a burn at all:

* samples go to a **dedicated raw-flash region** just below the LittleFS
  area,
* every **256-byte page** carries a magic number, page index, sample
  count and a **CRC32**,
* the slot for the next burn is **erased in advance** while the stand is
  idle, so during the burn only page programming happens (~400 µs), never
  a sector erase (~50 ms),
* an **A/B journal** in two sectors holds the coarse state (live slot,
  burn count, calibration); during rotation at least one copy is always
  valid.

**The worst a power cut can cost you is one page: 29 samples, about
360 ms at 80 SPS.** Everything older is already safely on flash.

### Resuming after a reset

If the journal says a burn was live when the stand booted, the firmware:

1. **leaves the pyro channel off** — we do not know whether the motor is
   already burning, and pushing current into the igniter of a burning
   motor is not something to do automatically,
2. finds the last intact page in the slot,
3. **resumes recording** for the rest of the window and marks the first
   new page as "gap of unknown length",
4. the analysis then reports that as a warning rather than hiding it.

### Safety

* `PIN_IGNITION` is driven LOW as the **very first** thing in `setup()`,
  before `pinMode()`, so the pin cannot glitch high at boot.
* One function, `pyroSet()`, is allowed to drive the gate, and it
  re-checks state, the RBF key and the time window itself — regardless of
  what the caller believes.
* An **independent hard cut-off** at `IGNITION_MAX_MS`: even if the main
  condition were somehow skipped, the gate cannot stay live longer.
* 4 s hardware watchdog.
* Countdown aborts on: RBF key inserted, button, ABORT in the web UI,
  loss of igniter continuity, load cell failure.
* The physical button must be **held** for 750 ms; a tap does nothing.
* The web code is compared in constant time, with a one-minute lockout
  after 5 wrong attempts.
* The countdown will not start without igniter continuity.

> **One thing the firmware cannot cover:** the MOSFET gate needs a
> **10 kΩ pulldown to GND**. On reset the GPIO reverts to an input and
> the gate floats — that state is only reachable by hardware.

### Measurement

* Own non-blocking HX711 driver instead of the stock library: it reports
  sensor failure, timestamps each conversion to tens of microseconds, and
  never hangs when the chip stops answering.
* **Raw ADC counts** are stored, not converted newtons. Calibration and
  tare live in each burn's header, so thrust can be **recomputed later**
  if the calibration turns out to have been wrong.
* **3 s of pre-roll** before the command (the original captured 5
  samples, about 50 ms). The baseline and its noise come from there, and
  without them there is nothing to derive the ignition detection
  threshold or the propellant mass from.
* The record window is 10 s. That covers a 4 s burn plus a couple of
  seconds of ignition delay plus the tail — the real measurement below
  showed a 1.7 s delay, which made the original 5 s window tight.

### Web UI

Live thrust plot, countdown, storage status, ABORT and TARE buttons, and
the reason the last sequence was aborted. Core 1 (the web server)
**never touches flash or the igniter** — it can only raise a request that
core 0 validates against the physical interlocks.

---

## 2. Wiring

| Pico pin | Function | Note |
|---|---|---|
| GP2 | HX711 DT | |
| GP3 | HX711 SCK | |
| GP6 | FIRE button | to 3V3, `INPUT_PULLDOWN` |
| GP11 | RBF key | HIGH = inserted = safe |
| GP21 | MOSFET gate | **10 kΩ pulldown to GND required** |
| GP22 | SK6812 / WS2812 | status pixel |
| GP28 | igniter continuity | HIGH = igniter connected |

Powered from a 2S LiPo.

Keep the pyro circuit isolated from the logic, or at least give it its
own supply path — igniter current running through the measurement ground
will show up on your thrust curve.

**Tie the HX711 RATE pin to VCC.** Left floating it runs at 10 SPS and
your thrust curve will be a handful of dots.

### Status pixel

| Colour | Meaning |
|---|---|
| blue | idle, RBF key inserted, safe |
| yellow | idle, key removed, continuity OK — armed |
| amber | idle, key removed, **no igniter continuity** |
| blinking red | countdown, blink rate increases as T0 approaches |
| solid red | recording, pyro window |
| cyan | dumping data over serial |
| blinking magenta | fault — no storage or no load cell |

---

## 3. Installing

### Firmware

1. Install [arduino-pico](https://github.com/earlephilhower/arduino-pico)
   in the Arduino IDE board manager.
2. Board: **Raspberry Pi Pico 2 W**.
3. Library needed: **Adafruit NeoPixel**. `WiFi`, `WebServer` and
   `LittleFS` come with the core.
4. Open `firmware/StaticFire_Stand/StaticFire_Stand.ino` and upload.

All the settings you are likely to change are in `config.h`: pin map,
sequence timing, SSID, password, arming code, number of slots.

**Change `AP_PASSWORD` and `SECRET_CODE` before the first live firing.**

### Python tools

```
pip install -r tools/requirements.txt
```

That pulls in numpy, pandas, matplotlib, openpyxl and pyserial. On Linux,
the graphical tool also needs tkinter, which is a separate OS package
there (already bundled with Python on Windows and macOS):

```
sudo apt install python3-tk        # Debian / Ubuntu
sudo dnf install python3-tkinter   # Fedora
```

---

## 4. Operating tutorial

### 4.0 Two ways to run the tools

**Graphical (recommended for most people).** Double-click
`tools/sf_gui.py`, or run:

```
python tools/sf_gui.py
```

This opens a window with two tabs:

* **Extract & Analyse** — download data from the stand, analyse a raw
  dump saved earlier, import an old V0-firmware CSV, or run the
  no-hardware demo. Pick the serial port from the dropdown, optionally
  type the propellant mass in grams, choose whether to generate charts,
  and press the button for what you want to do. Progress and any
  warnings appear in the **Log** pane at the bottom, and an **Open
  results folder** button lights up once a run finishes.
* **Stand Tools** — status, tare (zero the load cell) and calibration,
  the same operations available from the console menu below.

Nothing here changes what the tools compute — the graphical window calls
the exact same download and analysis code as the console script, it just
saves you from typing menu numbers.

**Console / IDLE (works everywhere, no extra package needed).** Open
`tools/static_fire.py` in Python IDLE and press **F5** for a text menu,
or run it from a terminal — see `python tools/static_fire.py --help` for
every flag. The rest of this tutorial shows the console menu numbers;
in the graphical tool, use the equivalently named button instead.

### 4.1 First time only: calibrate

Calibration turns ADC counts into newtons. Do it once per mechanical
build, and again any time you change the mount.

1. Connect the stand to the computer over USB.
2. Open `tools/static_fire.py` in **Python IDLE** and press **F5**, or
   open `tools/sf_gui.py` and switch to the **Stand Tools** tab.
3. Choose **5) Zero (tare) the load cell** (console) or **Zero (tare) the
   load cell** (GUI) with the stand empty (motor mount in place, no
   motor).
4. Put a known weight where the motor will push. Use something in the
   region of the thrust you expect — a 100 g weight calibrating a 250 N
   motor gives a poor factor.
5. Choose **6) Calibrate with a known weight** (console) or **Calibrate
   with a known weight...** (GUI) and enter the weight in grams.
6. Choose **4) Show stand status** (console) or **Show stand status**
   (GUI) and check that `cal_counts_per_n` is no longer 1.0.

The factor is stored on the Pico and survives power cycles. It is also
written into every burn's header.

### 4.2 Before every firing

Run through this at the bench, before anything is live:

- [ ] **RBF key inserted.** Status pixel is blue.
- [ ] Battery charged. A brownout mid-burn now costs you one page instead
      of the whole record, but it still ends the test.
- [ ] Motor bolted into the mount, nozzle pointing somewhere safe with
      nothing in the exhaust path.
- [ ] Nothing resting on the motor, no cables pulling on it — anything
      touching the motor shows up as thrust.
- [ ] Igniter installed in the motor but **not yet connected**.
- [ ] Load cell zeroed for this session (menu option 5).

### 4.3 Firing

1. **Power up the stand.** The pixel goes blue (safe).
2. **Connect to the WiFi access point** `SpaceCarrots_Stand` from a phone
   or laptop, then open `http://192.168.42.1/` in a browser. The page
   shows live thrust, continuity, RBF state and the storage status.
3. **Connect the igniter leads** to the pyro terminals. This is the last
   thing anyone does at the stand.
4. **Walk to the firing position.** Take the RBF key with you.
5. **Check continuity on the web page.** It must read `OK`. If it reads
   `OPEN` the igniter is not connected properly — this is exactly why you
   check from a distance, not at the stand.
6. **Check the storage line.** It should say how many slots are free. If
   it says `DISABLED`, stop: the burn will not be recorded.
7. **Remove the RBF key.** The pixel turns yellow, the web page shows
   `OUT / ARMED`, and the FIRE button becomes clickable.
8. **Confirm everyone is clear.** Call it out loud.
9. **Type the code and press START COUNTDOWN.** Confirm the dialog.
10. **The countdown runs for 15 s.** The pixel blinks red, faster as T0
    approaches, and the web page counts down `T-14.3 s`. The recorder
    opens 3 s before T0 and starts banking baseline samples.
11. **T0.** The igniter is energised for 3 s or until the hard cut-off.
    Recording continues for 10 s.
12. **The pixel goes back to blue/yellow** and the web page returns to
    `IDLE`. The burn is on flash.

**To abort at any point during the countdown:** press ABORT on the web
page, or insert the RBF key, or hold the physical button. Any of the
three stops the sequence and returns the stand to idle. Losing igniter
continuity aborts it automatically.

### 4.4 If the motor does not light

1. **Wait at least 60 seconds.** Hangfires exist. This is not optional.
2. Walk to the stand and **insert the RBF key first**, before touching
   anything else.
3. Disconnect the igniter leads.
4. Only then inspect the motor.

The stand will have recorded the whole attempt, which is useful: the
thrust curve tells you whether nothing happened at all or whether the
motor produced a little pressure and stopped.

### 4.5 After firing: getting the data

1. Insert the RBF key, disconnect the pyro leads, let the motor cool.
2. Bring the Pico to the computer and connect it over USB.
3. Open `tools/sf_gui.py` (or `tools/static_fire.py` in IDLE and press
   **F5** for the console menu instead).
4. **GUI:** on the **Extract & Analyse** tab, pick the serial port from
   the dropdown (the Pico is usually auto-selected), optionally type the
   **propellant mass in grams**, and press **Download from stand &
   analyse**.
   **Console:** choose **1) Download data from the stand and analyse
   it**, pick the port from the list, then enter the propellant mass
   when asked.
5. The propellant mass makes Isp trustworthy. Leaving it empty falls back
   to estimating it from how much the resting reading drops, which only
   works if the stand settles back to rest after the burn.
6. The tool downloads, **saves the raw dump to disk first**, then
   analyses every burn and writes the charts and spreadsheets. Progress
   shows in the GUI's log pane or the console window.
7. **GUI:** click **Open results folder** when it lights up, or answer
   the "open the folder now?" prompt.
   **Console:** answer `y` to open the results folder.

Results land in `results/YYYYMMDD_HHMMSS/` next to the tools folder
(or wherever you set as the output folder in the GUI).

**Erase the stand's memory only after you have checked the data.** It
holds 6 burns and overwrites the oldest automatically, so there is
rarely a reason to erase at all.

### 4.6 Reading the results

Each burn produces:

```
results/20260716_135131/
├── raw_dump.txt                 what the stand sent (keep this)
├── burn_001_data.csv            raw samples, Excel-ready
├── burn_001_data.xlsx           Summary + Data + Rise sheets, with a chart
├── burn_001_summary.csv         just the summary table
├── burn_001_charts.png          five charts on one sheet
├── all_burns_overview.csv       one row per burn, for comparing batches
└── burn_comparison.png          every curve overlaid
```

The chart sheet has five panels:

| Panel | What to look at |
|---|---|
| Thrust curve | the whole burn, with the pyro window, action time and the mass-loss correction line |
| Ignition close-up | the delay from command to first motion, and the rise thresholds |
| Cumulative impulse | how the impulse accumulates; a straight ramp means a neutral grain |
| Thrust build-up | percent of peak against time, in milliseconds |
| Record quality | interval between samples — flat is good, spikes are dropouts |

**Read the warnings.** When something is off — record cut short, load
cell saturated, mass loss unmeasurable, motor still burning at the end —
the script says so instead of printing a confident wrong number.

### 4.7 Reading old data

CSV files from the original firmware still work:

- GUI: **Extract & Analyse** tab → **Import a legacy V0 CSV...** (pick
  one or several files at once)
- IDLE: menu option **3) Analyse a CSV from the old V0 firmware**
- command line: `python static_fire.py --legacy-csv burn_1.csv`

Those files hold converted newtons rather than raw counts, so they
cannot be recalibrated — but every timing and impulse figure works.

### 4.8 Trying the tools without hardware

GUI: **Extract & Analyse** tab → **Run demo (no hardware needed)**.
Console: menu option **8) Demo**. Both generate a realistic fake dataset
and run the whole pipeline on it. Useful for learning what the outputs
look like before you have a motor on the stand.

---

## 5. Serial commands

The tools do all of this for you, but the console is there at 115200 baud
if you want it:

```
?           help
i           device status as JSON
t           zero the load cell
cal <N>     calibrate with a known force in newtons
calg <g>    calibrate with a known mass in grams
l           list slots
p           dump every stored burn
p <slot>    dump one slot
d           erase all data
abort       abort the running countdown
s           one thrust reading
```

---

## 6. What the analysis computes

**Ignition timing**, all measured from the fire command:

* delay to first motion — the threshold is `max(6σ of noise, 1 % of
  peak)` and three consecutive samples must clear it, so a single spike
  cannot move the result
* times to 5 / 10 / 25 / 50 / 75 / 90 / 95 / 100 % of peak, for both the
  rise and the decay
* rise time 10 → 90 %, steepest rise in N/s
* when the pyro channel opened

**Thrust curve:**

* peak thrust and when it occurred, peak-to-average ratio
* burn time (T0 → decay through 5 %) and action time (5 % → 5 %)
* average thrust over the action time and from T0
* thrust centroid and the time at which half the impulse was delivered

**Impulse and efficiency:**

* total impulse by trapezoidal integration
* **mass-loss correction**: as propellant leaves, the resting reading
  drops. The shift is interpolated against the impulse already delivered
  rather than linearly in time, because propellant does not leave at a
  constant rate
* propellant mass from the shift in resting reading, or entered manually
* Isp and effective exhaust velocity
* NAR/TRA letter class and a designation like `H169`

**Measurement quality** — sample rate, gaps, baseline noise and drift,
ADC saturation, pages dropped on CRC, missing closing page, resumes after
a restart.

---

## 7. Notes from a real measurement

A burn from 16 July 2026, recorded with the old firmware and re-analysed
through `--legacy-csv`, showed three things worth remembering:

* **The motor lit 1.715 s after the command** — 718 ms *after* the
  firmware had already cut the igniter. The pyrogen was burning on its
  own by then. Do not assume the motor lights when you press the button.
* **Resting reading was +0.23 N before and −2.26 N after.** The stand
  does not sit the same way afterwards. Before trusting a propellant mass
  derived from that shift, weigh the motor and pass the number in.
* **A 107 ms gap right at T0**, from the old `while (millis() <
  planned_ignition_time)` spin. The new firmware samples through that
  window.

---

## 8. Safety minimum

This is a device whose whole purpose is to set fire to a rocket motor.

1. RBF key inserted whenever anyone is closer than the safe distance.
2. The igniter is connected **last**, on the way out.
3. Continuity is checked from the firing position over WiFi, never at the
   stand.
4. After a failure to ignite, **wait at least 60 s**, then approach and
   insert the RBF key before anything else.
5. Point the nozzle where the exhaust has somewhere to go, and assume the
   casing can burst.
6. Fire extinguisher within reach, no children nearby, and never alone.

Software can only stop current reaching the igniter at the wrong moment.
The rest is on you.
