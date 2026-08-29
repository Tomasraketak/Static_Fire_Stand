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
#define BTN_HOLD_MS              750UL // physical button must be held this long
#define WDT_TIMEOUT_MS           4000  // hardware watchdog
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
