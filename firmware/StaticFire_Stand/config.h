// =====================================================================
//  Static Fire Stand - build time configuration
//  Everything you are likely to change lives in this one file.
// =====================================================================
#pragma once

// ---------------------------------------------------------------------
//  PIN MAP  (Raspberry Pi Pico 2 W)
// ---------------------------------------------------------------------
#define PIN_HX711_DT   2    // HX711 DOUT
#define PIN_HX711_SCK  3    // HX711 PD_SCK
#define PIN_BTN        6    // physical FIRE button, active HIGH, INPUT_PULLDOWN
#define PIN_RBF       11    // Remove-Before-Flight key, HIGH = key inserted = SAFE
#define PIN_IGNITION  21    // gate of the ignition MOSFET (needs 10k gate pulldown!)
#define PIN_LED       22    // SK6812 / WS2812 status pixel
#define PIN_CONT      28    // igniter continuity sense, HIGH = igniter present
#define PIN_BATT_ADC  27    // battery voltage sense, through the divider below (ADC1)

// ---------------------------------------------------------------------
//  SEQUENCE TIMING
// ---------------------------------------------------------------------
#define COUNTDOWN_MS      15000UL   // armed -> T0
#define PREROLL_MS         3000UL   // baseline recorded before T0 (negative times)

// Sized for a motor that burns at most 4 s. The window has to cover the
// ignition delay as well, not just the burn: a real measurement on this
// stand showed the motor lighting 1.7 s after the command, i.e. 0.7 s
// after the igniter had already been cut. 10 s leaves room for a ~2 s
// delay, a 4 s burn and the tail, with margin to spare. Raise it if you
// ever fire something longer - a slot holds ~92 s at 80 SPS.
#define RECORDING_MS      10000UL   // recorded after T0

// How long the pyro channel is energised. This is only the FALLBACK used
// on a virgin device or if the stored setting is out of range - the value
// actually used at runtime lives in flash (SfJournal.ignition_ms) and can
// be changed without reflashing: serial command `ign <ms>`, the web UI's
// settings card, or the Python GUI / `static_fire.py --ignition <ms>`.
#define DEFAULT_IGNITION_MS  300UL
#define IGNITION_MAX_MS     5000UL   // hard ceiling, enforced independently regardless of setting

// ---------------------------------------------------------------------
//  SAFETY
// ---------------------------------------------------------------------
#define REQUIRE_CONTINUITY_TO_ARM  0   // refuse to arm without igniter continuity
#define ABORT_ON_CONT_LOSS         0   // abort countdown if continuity disappears
#define BTN_HOLD_MS              500UL // physical button must be held this long
// After the button triggers, it is not looked at AT ALL for this long, and
// then has to be released before it counts again. The same press starting a
// countdown must not be able to abort it a millisecond later.
// Note this is the only thing the button is deaf to - ABORT in the web UI
// and the RBF key both still stop the sequence instantly.
#define BTN_LOCKOUT_MS          2000UL
#define WDT_TIMEOUT_MS           4000  // hardware watchdog

// ---------------------------------------------------------------------
//  KEEPING THE SAMPLE DROPOUTS OUT OF THE BURN
//
//  Something on this board stalls core 0 for a few hundred milliseconds
//  at a time, and it repeats on its own clock (~7.45 s on the stand this
//  was measured on) rather than in step with anything the sequence does.
//  Whenever one lands during the burn it eats ~30 samples and the
//  analysis reports a "largest gap between samples" warning.
//
//  The stalls cannot be prevented from here, but they can be dodged: the
//  firmware times its own sampling, learns the period, and then picks a
//  countdown length that puts the protected window between two of them.
//  The countdown shown to the operator is the adjusted one, so it still
//  counts down to the real T0 and reaches zero exactly at ignition -
//  nothing fires late or unannounced.
//
//  Set STALL_AVOIDANCE to 0 to switch it off and always use COUNTDOWN_MS
//  exactly. Nothing else changes; a dodge is only ever attempted when a
//  stable period has actually been measured recently.
#define STALL_AVOIDANCE             1
#define STALL_PROTECT_MS       4000UL  // keep T0 .. T0+this clear of a stall
#define STALL_GUARD_MS          500UL  // extra margin either side of the window
#define STALL_MAX_SHIFT_MS     8000UL  // never delay T0 by more than this
#define STALL_MIN_MS            100UL  // a sample gap this big counts as a stall
#define STALL_PERIOD_MIN_MS    1000UL  // plausible range for the repeat period
#define STALL_PERIOD_MAX_MS   60000UL
#define STALL_PERIOD_TOL_MS     400UL  // two periods this close count as agreeing
#define STALL_FRESH_MS        120000UL // ignore a pattern not seen for this long
#define MAX_FAILED_CODES            5  // web lockout after N wrong codes
#define LOCKOUT_MS              60000UL

// ---------------------------------------------------------------------
//  LOAD CELL
// ---------------------------------------------------------------------
#define HX711_GAIN             128     // 128 or 64 (ch A), 32 (ch B)
#define HX711_TIMEOUT_US    500000UL   // no conversion in this long => sensor fault
#define TARE_SAMPLES              32

// Fallback calibration factor (raw ADC counts per newton), used on a
// virgin device or if the stored value is invalid. This is only a
// starting point - always calibrate the real stand with a known weight
// (or type the factor in directly, if you already know it) before
// trusting the numbers: serial `cal`/`calg`/`calset`, the Python GUI's
// "Stand Tools" tab, or `static_fire.py --calibrate` / `--cal-factor`.
#define DEFAULT_CAL_COUNTS_PER_N  2291.9275f

// ---------------------------------------------------------------------
//  BATTERY VOLTAGE
//  A resistive divider from the battery (2S LiPo, ~6-8.4 V) brings the
//  pack voltage down into the RP2040 ADC's 0-3.3 V range on PIN_BATT_ADC:
//
//      VBATT --[ R_TOP ]--+--[ R_BOTTOM ]-- GND
//                          |
//                     PIN_BATT_ADC
//
//  BATT_DIVIDER_RATIO is (R_TOP + R_BOTTOM) / R_BOTTOM. The default below
//  matches R_TOP = 20 kOhm (to VBATT) / R_BOTTOM = 10 kOhm (to GND), ratio
//  3.0, giving ~2.8 V at the ADC for a fully charged 8.4 V pack -
//  comfortably inside range. Recompute it for your own resistors, or
//  nudge it to match a multimeter reading on VBATT. Set
//  BATT_DIVIDER_RATIO to 0 to disable the reading entirely (hides it
//  from the web UI and serial info) if the divider isn't populated.
#define BATT_DIVIDER_RATIO   3.0f
#define BATT_WARN_MV         7000     // 2S LiPo getting low - amber in the web UI
#define BATT_CRIT_MV         6600     // 2S LiPo close to over-discharge - red

// ---------------------------------------------------------------------
//  NETWORK
// ---------------------------------------------------------------------
#define AP_SSID     "SpaceCarrots_Stand"
#define AP_PASSWORD "thrust123"          // >= 8 chars. CHANGE THIS.
#define SECRET_CODE "31415"              // default arming code typed in the web UI. CHANGE THIS.

// ---------------------------------------------------------------------
//  FLASH DATA LOG
//  The log lives in raw flash directly below the LittleFS area, so a
//  reset in the middle of a burn can never corrupt a filesystem.
// ---------------------------------------------------------------------
#define LOG_SLOTS          6            // number of burns kept on the device
#define LOG_SLOT_BYTES     (64u * 1024u)
#define LOG_JOURNAL_BYTES  (2u * 4096u) // two sectors, A/B rotated

#define FW_VERSION "2.0.0"
#define BAUD_RATE  115200
