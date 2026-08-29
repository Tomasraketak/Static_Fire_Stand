// =====================================================================
//  Space Carrots - solid motor static fire test stand
//  Raspberry Pi Pico 2 W / arduino-pico core
//
//  Core 0 : safety interlocks, load cell sampling, ignition timing,
//           power-fail tolerant flash recorder, serial console
//  Core 1 : WiFi access point and web UI (never touches flash)
//
//  See docs/DATA_FORMAT.md for the on-flash and on-wire formats and
//  README.md for wiring and the safety checklist.
// =====================================================================
#include <Arduino.h>
#include <WiFi.h>
#include <WebServer.h>
#include <Adafruit_NeoPixel.h>
#include <hardware/watchdog.h>

#include "config.h"
#include "hx711_fast.h"
#include "flashlog.h"

// ---------------------------------------------------------------------
//  State machine
// ---------------------------------------------------------------------
enum StandState : uint32_t {
  ST_BOOT      = 0,
  ST_IDLE      = 1,
  ST_COUNTDOWN = 2,
  ST_RECORDING = 3,
  ST_FAULT     = 4,
  ST_DUMPING   = 5,
};

// Every cross-core variable is a single 32 bit word, so reads and writes
// are atomic on Cortex-M and no lock is needed. Nothing wider is shared.
volatile uint32_t tlm_state        = ST_BOOT;
volatile int32_t  tlm_thrust_mN    = 0;
volatile int32_t  tlm_raw          = 0;
volatile uint32_t tlm_cont         = 0;
volatile uint32_t tlm_rbf          = 1;
volatile uint32_t tlm_pyro         = 0;
volatile int32_t  tlm_t_ms         = 0;   // countdown remaining, or time since T0
volatile uint32_t tlm_samples      = 0;
volatile uint32_t tlm_total_burns  = 0;
volatile uint32_t tlm_free_slots   = 0;
volatile uint32_t tlm_log_ok       = 0;
volatile uint32_t tlm_hx_ok        = 0;
volatile uint32_t tlm_boot_count   = 0;
volatile uint32_t tlm_resume_count = 0;
volatile uint32_t tlm_last_abort   = 0;   // reason code of the last abort

// requests raised by core 1 (web) and consumed by core 0
volatile uint32_t req_fire         = 0;
volatile uint32_t req_abort        = 0;
volatile uint32_t req_tare         = 0;
volatile uint32_t req_set_ignition = 0;
volatile uint32_t req_ignition_ms  = 0;
volatile uint32_t tlm_ignition_ms  = 0;   // current igniter on-time, for the web UI

// web brute force lockout
volatile uint32_t web_fails   = 0;
volatile uint32_t web_lock_ms = 0;

enum AbortReason : uint32_t {
  AB_NONE = 0, AB_RBF, AB_BUTTON, AB_WEB, AB_CONT_LOSS, AB_SENSOR, AB_STORAGE, AB_RESET
};
static const char *abortName(uint32_t r) {
  switch (r) {
    case AB_RBF:       return "RBF inserted";
    case AB_BUTTON:    return "button abort";
    case AB_WEB:       return "web abort";
    case AB_CONT_LOSS: return "continuity lost";
    case AB_SENSOR:    return "load cell fault";
    case AB_STORAGE:   return "storage fault";
    case AB_RESET:     return "unexpected reset";
    default:           return "none";
  }
}

HX711Fast scale;
Adafruit_NeoPixel pixels(1, PIN_LED, NEO_GRB + NEO_KHZ800);
WebServer server(80);

// ---------------------------------------------------------------------
//  Runtime state (core 0 only)
// ---------------------------------------------------------------------
static StandState state = ST_BOOT;
static float    calCountsPerN = DEFAULT_CAL_COUNTS_PER_N;   // raw counts per newton
static uint32_t ignitionMs    = DEFAULT_IGNITION_MS;         // igniter on-time, ms
static int32_t  tareOffset    = 0;
static uint32_t t_state_entry = 0;      // millis() when the state was entered
static uint32_t t0_us         = 0;      // micros() at the ignition command
static bool     pyroOn        = false;
static uint32_t pyroOnUs      = 0;
static uint32_t pyroOffUs     = 0;
static uint32_t burnSlot      = 0;
static uint32_t burnId        = 0;
static bool     prerollOpen   = false;
static bool     resumedBurn   = false;
static uint32_t eraseCursor   = 0;
static uint32_t eraseSlotIdx  = 0xFFFFFFFFu;
static uint32_t btnDownSince  = 0;
static uint32_t lastLedUpdate = 0;
static bool     dumping       = false;

static inline float rawToN(int32_t raw) {
  if (calCountsPerN == 0.0f) return 0.0f;
  return (float)(raw - tareOffset) / calCountsPerN;
}

// ---------------------------------------------------------------------
//  Ignition output - the single place that is allowed to drive the gate
// ---------------------------------------------------------------------
static void pyroSet(bool on) {
  if (on) {
    // Belt and braces: three independent conditions, all checked here,
    // regardless of what the caller thinks it is doing.
    if (state != ST_RECORDING) return;
    if (digitalRead(PIN_RBF) == HIGH) return;
    if ((uint32_t)(micros() - t0_us) > IGNITION_MAX_MS * 1000UL) return;
  }
  digitalWrite(PIN_IGNITION, on ? HIGH : LOW);
  pyroOn = on;
  tlm_pyro = on ? 1 : 0;
}

static void pyroForceOff() {
  digitalWrite(PIN_IGNITION, LOW);
  pyroOn = false;
  tlm_pyro = 0;
}

// ---------------------------------------------------------------------
//  Status pixel
// ---------------------------------------------------------------------
static void setLED(uint8_t r, uint8_t g, uint8_t b) {
  pixels.setPixelColor(0, pixels.Color(r, g, b));
  pixels.show();
}

static void updateLED() {
  // The pixel protocol masks interrupts for ~30 us per update, so it is
  // rate limited and never runs inside the ignition window.
  uint32_t now = millis();
  if (now - lastLedUpdate < 40) return;
  lastLedUpdate = now;

  switch (state) {
    case ST_FAULT:                                    // magenta
      setLED(((now / 250) % 2) ? 180 : 0, 0, ((now / 250) % 2) ? 60 : 0); break;
    case ST_IDLE:
      if (tlm_rbf)          setLED(0, 0, 120);        // blue  : safe
      else if (!tlm_cont)   setLED(120, 40, 0);       // amber : armed, no igniter
      else                  setLED(160, 160, 0);      // yellow: armed
      break;
    case ST_COUNTDOWN: {                              // red, blink rate ramps up
      int32_t rem = tlm_t_ms;
      uint32_t period = rem > 5000 ? 500 : (rem > 2000 ? 200 : 80);
      setLED(((now / period) % 2) ? 255 : 0, 0, 0);
      break;
    }
    case ST_RECORDING:  setLED(255, 0, 0); break;     // solid red
    case ST_DUMPING:    setLED(0, 80, 120); break;    // cyan
    default:            setLED(0, 60, 0); break;      // dim green
  }
}

// ---------------------------------------------------------------------
//  Sampling
// ---------------------------------------------------------------------
static uint32_t sensorFaultSince = 0;

// Reads the load cell if a conversion is waiting. When a burn is open the
// sample is appended to the flash log. Returns true if a sample was taken.
static bool serviceLoadCell() {
  if (!scale.ready()) {
    if (scale.faulted(HX711_TIMEOUT_US)) {
      tlm_hx_ok = 0;
      if (!sensorFaultSince) sensorFaultSince = millis();
    }
    return false;
  }
  uint32_t t_us = micros();
  int32_t raw   = scale.readRaw();
  tlm_hx_ok = 1;
  sensorFaultSince = 0;

  tlm_raw = raw;
  tlm_thrust_mN = (int32_t)(rawToN(raw) * 1000.0f);

  if (flashLog.recording() && prerollOpen) {
    uint8_t f = 0;
    if (pyroOn)                     f |= SF_SF_PYRO;
    if (digitalRead(PIN_CONT))      f |= SF_SF_CONT;
    if (digitalRead(PIN_RBF))       f |= SF_SF_RBF;
    if (raw >= 0x7FFFFE || raw <= -0x7FFFFE) f |= SF_SF_SAT;
    flashLog.addSample((int32_t)(t_us - t0_us), raw, f);
    tlm_samples = flashLog.sampleCount();
  }
  return true;
}

// ---------------------------------------------------------------------
//  Base64 for the serial dump
// ---------------------------------------------------------------------
static const char B64[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

// 256 raw bytes -> 344 base64 chars, emitted as one write so the USB
// stack sees a single packet burst instead of 86 tiny ones.
static void printBase64Page(const uint8_t *data) {
  static char out[SF_PAGE_SIZE / 3 * 4 + 8];
  size_t o = 0;
  for (size_t i = 0; i < SF_PAGE_SIZE; i += 3) {
    uint32_t v = (uint32_t)data[i] << 16;
    if (i + 1 < SF_PAGE_SIZE) v |= (uint32_t)data[i + 1] << 8;
    if (i + 2 < SF_PAGE_SIZE) v |= data[i + 2];
    out[o++] = B64[(v >> 18) & 63];
    out[o++] = B64[(v >> 12) & 63];
    out[o++] = (i + 1 < SF_PAGE_SIZE) ? B64[(v >> 6) & 63] : '=';
    out[o++] = (i + 2 < SF_PAGE_SIZE) ? B64[v & 63] : '=';
  }
  out[o++] = '\n';
  Serial.write((const uint8_t *)out, o);
}

static void printInfoJson() {
  Serial.printf("{\"fw\":\"%s\",\"boots\":%lu,\"resumes\":%lu,\"burns\":%lu,"
                "\"slots\":%lu,\"pages_per_slot\":%lu,\"samples_per_page\":%u,"
                "\"cal_counts_per_n\":%.6f,\"tare\":%ld,\"log_ok\":%d,\"log_err\":\"%s\","
                "\"preroll_ms\":%lu,\"recording_ms\":%lu,\"ignition_ms\":%lu,"
                "\"countdown_ms\":%lu,\"state\":%lu}\n",
                FW_VERSION,
                (unsigned long)flashLog.journal().boot_count,
                (unsigned long)flashLog.journal().resume_count,
                (unsigned long)flashLog.journal().total_burns,
                (unsigned long)flashLog.slotCount(),
                (unsigned long)flashLog.pagesPerSlot(),
                (unsigned)SF_SAMPLES_PER_PAGE,
                calCountsPerN, (long)tareOffset,
                flashLog.ok() ? 1 : 0, flashLog.error(),
                (unsigned long)PREROLL_MS, (unsigned long)RECORDING_MS,
                (unsigned long)ignitionMs, (unsigned long)COUNTDOWN_MS,
                (unsigned long)state);
}

// Dumps raw 256 byte pages as base64, one line per page. Every page
// carries its own CRC, so a corrupted line is dropped by the host rather
// than poisoning the whole download.
static void dumpSlots(int onlySlot) {
  dumping = true;
  StandState prev = state;
  state = ST_DUMPING;
  tlm_state = ST_DUMPING;

  Serial.println("#BEGIN SFDUMP v2");
  Serial.print("#INFO ");
  printInfoJson();

  for (uint32_t s = 0; s < flashLog.slotCount(); s++) {
    if (onlySlot >= 0 && (uint32_t)onlySlot != s) continue;
    uint32_t pages = flashLog.validPages(s);
    if (pages == 0) continue;
    Serial.printf("#SLOT %lu pages=%lu\n", (unsigned long)s, (unsigned long)pages);
    for (uint32_t p = 0; p < pages; p++) {
      rp2040.wdt_reset();
      printBase64Page(flashLog.pagePtr(s, p));
    }
    Serial.printf("#ENDSLOT %lu\n", (unsigned long)s);
  }
  Serial.println("#END SFDUMP");

  state = prev;
  tlm_state = prev;
  dumping = false;
}

// ---------------------------------------------------------------------
//  Serial console
// ---------------------------------------------------------------------
static void printHelp() {
  Serial.println(F("commands:"));
  Serial.println(F("  i          device info as JSON"));
  Serial.println(F("  t          tare (zero) the load cell"));
  Serial.println(F("  cal <N>    calibrate with a known load in newtons"));
  Serial.println(F("  calg <g>   calibrate with a known mass in grams"));
  Serial.println(F("  calset <c> set the calibration factor directly, in counts/N"));
  Serial.println(F("  ign <ms>   set the igniter on-time in milliseconds"));
  Serial.println(F("  p          dump every stored burn"));
  Serial.println(F("  p <slot>   dump one slot"));
  Serial.println(F("  l          list slots"));
  Serial.println(F("  d          erase all burn data"));
  Serial.println(F("  abort      abort the current countdown"));
  Serial.println(F("  s          one-shot thrust reading"));
}

static void abortSequence(uint32_t reason);

static void handleSerial() {
  if (!Serial.available()) return;
  String cmd = Serial.readStringUntil('\n');
  cmd.trim();
  if (!cmd.length()) return;

  if (cmd == "?" || cmd == "help") {
    printHelp();
  } else if (cmd == "i") {
    printInfoJson();
  } else if (cmd == "s") {
    Serial.printf("raw=%ld thrust=%.3f N\n", (long)tlm_raw, tlm_thrust_mN / 1000.0f);
  } else if (cmd == "t") {
    if (state != ST_IDLE) { Serial.println("busy"); return; }
    int32_t off;
    if (scale.tare(TARE_SAMPLES, off)) {
      tareOffset = off;
      flashLog.journal().tare_offset = off;
      flashLog.commitJournal();
      Serial.printf("tared, offset=%ld\n", (long)off);
    } else {
      Serial.println("tare failed: load cell not responding");
    }
  } else if (cmd.startsWith("cal ") || cmd.startsWith("calg ")) {
    if (state != ST_IDLE) { Serial.println("busy"); return; }
    bool grams = cmd.startsWith("calg ");
    float known = cmd.substring(grams ? 5 : 4).toFloat();
    float knownN = grams ? (known / 1000.0f) * 9.80665f : known;
    if (knownN <= 0.0f) { Serial.println("need a positive load"); return; }
    int32_t v;
    if (!scale.tare(TARE_SAMPLES, v)) { Serial.println("load cell not responding"); return; }
    float counts = (float)(v - tareOffset);
    if (fabsf(counts) < 100.0f) { Serial.println("signal too small, check wiring"); return; }
    calCountsPerN = counts / knownN;
    flashLog.journal().cal_counts_per_n = calCountsPerN;
    flashLog.commitJournal();
    Serial.printf("calibrated: %.4f counts/N (%.4f counts/g)\n",
                  calCountsPerN, calCountsPerN * 9.80665f / 1000.0f);
  } else if (cmd.startsWith("calset ")) {
    if (state != ST_IDLE) { Serial.println("busy"); return; }
    float v = cmd.substring(7).toFloat();
    if (!(v > 0.0f) && !(v < 0.0f)) { Serial.println("value must be nonzero"); return; }
    calCountsPerN = v;
    flashLog.journal().cal_counts_per_n = calCountsPerN;
    flashLog.commitJournal();
    Serial.printf("calibration factor set to %.4f counts/N (%.4f counts/g)\n",
                  calCountsPerN, calCountsPerN * 9.80665f / 1000.0f);
  } else if (cmd.startsWith("ign ")) {
    if (state != ST_IDLE) { Serial.println("busy"); return; }
    long ms = cmd.substring(4).toInt();
    if (ms < 10 || (uint32_t)ms > IGNITION_MAX_MS) {
      Serial.printf("igniter on-time must be between 10 and %lu ms\n",
                    (unsigned long)IGNITION_MAX_MS);
      return;
    }
    ignitionMs = (uint32_t)ms;
    flashLog.journal().ignition_ms = ignitionMs;
    flashLog.commitJournal();
    Serial.printf("igniter on-time set to %lu ms\n", (unsigned long)ignitionMs);
  } else if (cmd == "l") {
    for (uint32_t s = 0; s < flashLog.slotCount(); s++) {
      uint32_t pages = flashLog.validPages(s);
      const SfHeaderPage *h = (const SfHeaderPage *)flashLog.pagePtr(s, 0);
      Serial.printf("slot %lu: pages=%lu burn_id=%lu ready=%d\n",
                    (unsigned long)s, (unsigned long)pages,
                    pages ? (unsigned long)h->burn_id : 0UL,
                    flashLog.slotIsReady(s) ? 1 : 0);
    }
  } else if (cmd == "p") {
    dumpSlots(-1);
  } else if (cmd.startsWith("p ")) {
    dumpSlots(cmd.substring(2).toInt());
  } else if (cmd == "d") {
    if (state != ST_IDLE) { Serial.println("busy"); return; }
    flashLog.eraseAll();
    tlm_total_burns = 0;
    Serial.println("all burn data erased");
  } else if (cmd == "abort") {
    if (state == ST_COUNTDOWN) abortSequence(AB_WEB);
    Serial.println("abort requested");
  } else {
    Serial.println("unknown command, try ?");
  }
}

// ---------------------------------------------------------------------
//  Sequence control
// ---------------------------------------------------------------------
static void enterState(StandState s) {
  state = s;
  tlm_state = s;
  t_state_entry = millis();
}

static void abortSequence(uint32_t reason) {
  pyroForceOff();
  if (flashLog.recording()) flashLog.endBurn(0, false);
  prerollOpen = false;
  tlm_last_abort = reason;
  Serial.printf("ABORT: %s\n", abortName(reason));
  enterState(ST_IDLE);
}

static bool interlocksOk(uint32_t &why) {
  if (digitalRead(PIN_RBF) == HIGH)          { why = AB_RBF;     return false; }
  if (!flashLog.ok())                        { why = AB_STORAGE; return false; }
  if (!tlm_hx_ok)                            { why = AB_SENSOR;  return false; }
#if REQUIRE_CONTINUITY_TO_ARM
  if (digitalRead(PIN_CONT) != HIGH)         { why = AB_CONT_LOSS; return false; }
#endif
  return true;
}

static void openBurnFile() {
  burnId   = flashLog.journal().total_burns + 1;
  burnSlot = (burnId - 1) % flashLog.slotCount();

  SfHeaderPage h;
  memset(&h, 0, sizeof(h));
  h.boot_count       = flashLog.journal().boot_count;
  h.fw_version       = (2u << 16) | (0u << 8) | 0u;
  h.cal_counts_per_n = calCountsPerN;
  h.tare_offset      = tareOffset;
  h.preroll_ms       = PREROLL_MS;
  h.recording_ms     = RECORDING_MS;
  h.ignition_ms      = ignitionMs;
  h.countdown_ms     = COUNTDOWN_MS;

  if (!flashLog.startBurn(burnSlot, burnId, h)) {
    abortSequence(AB_STORAGE);
    return;
  }
  prerollOpen = true;
  tlm_samples = 0;
  Serial.printf("recording opened: burn %lu -> slot %lu\n",
                (unsigned long)burnId, (unsigned long)burnSlot);
}

static void closeBurn(bool clean) {
  flashLog.endBurn(pyroOffUs, clean);
  prerollOpen = false;
  flashLog.journal().total_burns = burnId;
  flashLog.commitJournal();
  tlm_total_burns = burnId;
  Serial.printf("burn %lu closed: %lu samples, %s\n",
                (unsigned long)burnId, (unsigned long)flashLog.sampleCount(),
                clean ? "clean" : "TRUNCATED");
}

// ---------------------------------------------------------------------
//  Boot
// ---------------------------------------------------------------------
void setup() {
  // Kill the pyro output before anything else can go wrong. Setting the
  // level first means the pin never glitches high as it becomes an output.
  digitalWrite(PIN_IGNITION, LOW);
  pinMode(PIN_IGNITION, OUTPUT);
  digitalWrite(PIN_IGNITION, LOW);

  pinMode(PIN_BTN,  INPUT_PULLDOWN);
  pinMode(PIN_RBF,  INPUT_PULLDOWN);
  pinMode(PIN_CONT, INPUT_PULLDOWN);

  Serial.begin(BAUD_RATE);

  pixels.begin();
  pixels.setBrightness(120);
  setLED(0, 40, 0);

  bool wdtReboot = watchdog_caused_reboot();

  scale.begin(PIN_HX711_DT, PIN_HX711_SCK, HX711_GAIN);

  bool logOk = flashLog.begin();
  tlm_log_ok = logOk ? 1 : 0;
  if (!logOk) Serial.printf("FLASH LOG DISABLED: %s\n", flashLog.error());

  if (logOk) {
    calCountsPerN = flashLog.journal().cal_counts_per_n;
    if (!(calCountsPerN > 0.0f) && !(calCountsPerN < 0.0f)) calCountsPerN = DEFAULT_CAL_COUNTS_PER_N;
    tareOffset    = flashLog.journal().tare_offset;
    ignitionMs    = flashLog.journal().ignition_ms;
    if (ignitionMs < 10 || ignitionMs > IGNITION_MAX_MS) ignitionMs = DEFAULT_IGNITION_MS;
    tlm_ignition_ms = ignitionMs;
    flashLog.journal().boot_count++;
    tlm_boot_count   = flashLog.journal().boot_count;
    tlm_total_burns  = flashLog.journal().total_burns;
    tlm_resume_count = flashLog.journal().resume_count;
  }

  // Prime the sensor so tlm_hx_ok is meaningful before the first loop.
  int32_t v;
  if (scale.readBlocking(v, 1000000UL)) { tlm_hx_ok = 1; tlm_raw = v; }

  // ---- crash recovery -------------------------------------------------
  // If the journal says a burn was live, the previous run was cut short
  // by a brownout, a watchdog bite or a yanked battery.
  if (logOk && flashLog.journal().state == SF_STATE_RECORDING) {
    uint32_t slot = flashLog.journal().active_slot;
    uint32_t id   = flashLog.journal().total_burns + 1;
    int32_t  lastT = 0; uint32_t nS = 0;
    flashLog.journal().resume_count++;
    tlm_resume_count = flashLog.journal().resume_count;

    Serial.printf("RECOVERY: burn %lu in slot %lu was interrupted (%s reset)\n",
                  (unsigned long)id, (unsigned long)slot,
                  wdtReboot ? "watchdog" : "power");

    bool canResume = flashLog.resumeBurn(slot, id, lastT, nS);
    Serial.printf("RECOVERY: %lu samples survived, last t=%ld us\n",
                  (unsigned long)nS, (long)lastT);

    int32_t remaining = (int32_t)RECORDING_MS * 1000 - lastT;
    if (canResume && remaining > 500000) {
      // Pick the recording back up. The igniter is deliberately NOT
      // re-energised: we do not know whether the motor is already lit,
      // and re-firing into a burning motor is how people lose hands.
      burnId   = id;
      burnSlot = slot;
      resumedBurn = true;
      prerollOpen = true;
      // micros() restarts at 0, so anchor the timeline to the last
      // surviving sample. The gap of unknown length is flagged in the
      // first page we write, and the host reports it.
      t0_us = micros() - (uint32_t)lastT;
      pyroForceOff();
      pyroOffUs = (uint32_t)lastT;
      enterState(ST_RECORDING);
      Serial.println("RECOVERY: resuming recording, igniter stays OFF");
    } else {
      flashLog.endBurn(0, false);
      flashLog.journal().total_burns = id;
      tlm_total_burns = id;
      tlm_last_abort = AB_RESET;
      Serial.println("RECOVERY: burn closed as truncated");
    }
    flashLog.commitJournal();
  } else if (logOk) {
    flashLog.commitJournal();
  }

  if (state != ST_RECORDING) {
    if (!logOk) enterState(ST_FAULT);
    else        enterState(ST_IDLE);
  }

  rp2040.wdt_begin(WDT_TIMEOUT_MS);
  Serial.printf("Static Fire Stand %s ready. Type ? for help.\n", FW_VERSION);
}

// ---------------------------------------------------------------------
//  Main loop (core 0)
// ---------------------------------------------------------------------
void loop() {
  rp2040.wdt_reset();
  handleSerial();
  serviceLoadCell();

  const bool rbf  = (digitalRead(PIN_RBF)  == HIGH);
  const bool cont = (digitalRead(PIN_CONT) == HIGH);
  const bool btn  = (digitalRead(PIN_BTN)  == HIGH);
  tlm_rbf  = rbf ? 1 : 0;
  tlm_cont = cont ? 1 : 0;
  tlm_free_slots = 0;
  for (uint32_t s = 0; s < flashLog.slotCount(); s++)
    if (flashLog.slotIsReady(s)) tlm_free_slots++;

  // physical button: must be held, and only counts when the key is out
  bool btnFire = false;
  if (btn) {
    if (!btnDownSince) btnDownSince = millis();
    else if (millis() - btnDownSince > BTN_HOLD_MS) { btnFire = true; btnDownSince = millis() + 100000UL; }
  } else {
    btnDownSince = 0;
  }

  if (req_tare && state == ST_IDLE) {
    req_tare = 0;
    int32_t off;
    if (scale.tare(TARE_SAMPLES, off)) {
      tareOffset = off;
      flashLog.journal().tare_offset = off;
      flashLog.commitJournal();
    }
  }

  if (req_set_ignition && state == ST_IDLE) {
    req_set_ignition = 0;
    uint32_t ms = req_ignition_ms;
    if (ms >= 10 && ms <= IGNITION_MAX_MS) {
      ignitionMs = ms;
      flashLog.journal().ignition_ms = ignitionMs;
      flashLog.commitJournal();
    }
  }
  tlm_ignition_ms = ignitionMs;

  switch (state) {

    // -----------------------------------------------------------------
    case ST_FAULT: {
      req_fire = 0;
      if (flashLog.ok() && tlm_hx_ok) enterState(ST_IDLE);
      break;
    }

    // -----------------------------------------------------------------
    case ST_IDLE: {
      pyroForceOff();
      tlm_t_ms = 0;
      req_abort = 0;

      // Keep the next slot pre-erased so the countdown never has to wait
      // on a 50 ms sector erase, and a burn can start instantly.
      uint32_t next = flashLog.journal().total_burns % flashLog.slotCount();
      if (flashLog.ok() && !flashLog.slotIsReady(next)) {
        if (eraseSlotIdx != next) { eraseSlotIdx = next; eraseCursor = 0; }
        flashLog.eraseSlotStep(next, eraseCursor);
      } else {
        eraseSlotIdx = 0xFFFFFFFFu;
      }

      uint32_t why = AB_NONE;
      if (req_fire || btnFire) {
        req_fire = 0;
        if (interlocksOk(why)) {
          Serial.printf("COUNTDOWN started (%lu ms)\n", (unsigned long)COUNTDOWN_MS);
          tlm_last_abort = AB_NONE;
          resumedBurn = false;
          enterState(ST_COUNTDOWN);
        } else {
          Serial.printf("fire rejected: %s\n", abortName(why));
        }
      }
      break;
    }

    // -----------------------------------------------------------------
    case ST_COUNTDOWN: {
      uint32_t elapsed = millis() - t_state_entry;
      int32_t  rem     = (int32_t)COUNTDOWN_MS - (int32_t)elapsed;
      tlm_t_ms = rem;

      if (rbf)                     { abortSequence(AB_RBF);       break; }
      if (req_abort)               { req_abort = 0; abortSequence(AB_WEB); break; }
      if (btnFire)                 { abortSequence(AB_BUTTON);    break; }
      if (!tlm_hx_ok)              { abortSequence(AB_SENSOR);    break; }
#if ABORT_ON_CONT_LOSS
      if (!cont)                   { abortSequence(AB_CONT_LOSS); break; }
#endif

      // Erase early, while there is slack, one sector at a time.
      uint32_t next = flashLog.journal().total_burns % flashLog.slotCount();
      if (!flashLog.slotIsReady(next) && rem > (int32_t)PREROLL_MS + 2000) {
        if (eraseSlotIdx != next) { eraseSlotIdx = next; eraseCursor = 0; }
        flashLog.eraseSlotStep(next, eraseCursor);
        break;
      }

      // Open the log PREROLL_MS before T0 and start banking baseline
      // samples with negative timestamps.
      if (!prerollOpen && rem <= (int32_t)PREROLL_MS) {
        t0_us = micros() + (uint32_t)rem * 1000UL;
        openBurnFile();
        if (state != ST_COUNTDOWN) break;   // openBurnFile aborted
      }

      if (rem <= 0) {
        // ---- T0 -------------------------------------------------------
        enterState(ST_RECORDING);
        t0_us     = micros();
        pyroOnUs  = 0;
        pyroOffUs = 0;
        pyroSet(true);
        pyroOnUs = micros();
        Serial.println("T0 - IGNITION");
      }
      break;
    }

    // -----------------------------------------------------------------
    case ST_RECORDING: {
      uint32_t sinceT0Us = micros() - t0_us;
      tlm_t_ms = (int32_t)(sinceT0Us / 1000UL);

      // Independent hard cut-off. Even if the primary condition below is
      // somehow skipped, the gate cannot stay live past IGNITION_MAX_MS.
      if (pyroOn && sinceT0Us >= IGNITION_MAX_MS * 1000UL) {
        pyroForceOff();
        pyroOffUs = sinceT0Us;
        Serial.println("pyro hard cut-off");
      }
      if (pyroOn && sinceT0Us >= ignitionMs * 1000UL) {
        pyroSet(false);
        pyroOffUs = sinceT0Us;
      }

      if (flashLog.full()) {
        Serial.println("slot full, closing early");
        pyroForceOff();
        closeBurn(false);
        enterState(ST_IDLE);
        break;
      }

      if (sinceT0Us >= RECORDING_MS * 1000UL) {
        pyroForceOff();
        closeBurn(!resumedBurn);
        Serial.println("recording complete");
        enterState(ST_IDLE);
      }
      break;
    }

    default:
      enterState(ST_IDLE);
      break;
  }

  if (!dumping) updateLED();
}

// =====================================================================
//  CORE 1 - WiFi access point and web UI
//
//  This core never touches flash and never drives the igniter. It can
//  only raise a request flag that core 0 validates against the physical
//  interlocks.
// =====================================================================
const char html_page[] PROGMEM = R"rawliteral(
<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Static Fire Stand</title>
<style>
 :root{--bg:#0d0d0d;--card:#1a1a19;--ink:#fff;--mut:#898781;--line:#2c2c2a;
       --ok:#0ca30c;--warn:#fab219;--crit:#d03b3b;--acc:#3987e5}
 *{box-sizing:border-box}
 body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;background:var(--bg);
      color:var(--ink);margin:0;padding:16px;display:flex;flex-direction:column;
      align-items:center;gap:16px}
 h1{font-size:1.2rem;margin:0;color:var(--mut);font-weight:600;letter-spacing:.08em}
 .card{background:var(--card);border:1px solid var(--line);border-radius:12px;
       padding:16px;width:100%;max-width:520px}
 .big{font-size:3rem;font-weight:600;text-align:center;font-variant-numeric:tabular-nums}
 .unit{font-size:1rem;color:var(--mut);font-weight:400}
 .row{display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px solid var(--line);
      font-size:.95rem}
 .row:last-child{border-bottom:0}
 .row span:first-child{color:var(--mut)}
 .v{font-variant-numeric:tabular-nums;font-weight:600}
 .ok{color:var(--ok)}.warn{color:var(--warn)}.crit{color:var(--crit)}
 .state{text-align:center;font-size:1.4rem;font-weight:700;letter-spacing:.1em;padding:10px;
        border-radius:8px;background:#000;margin-bottom:12px}
 input,button{font:inherit;border-radius:8px;border:1px solid var(--line);padding:12px;width:100%}
 input{background:#000;color:var(--ink);text-align:center;letter-spacing:.4em}
 button{margin-top:10px;font-weight:700;cursor:pointer;color:#fff;border:0}
 .fire{background:var(--crit)} .abort{background:#444} .tare{background:#333}
 button:disabled{opacity:.35;cursor:not-allowed}
 canvas{width:100%;height:110px;display:block}
 .msg{text-align:center;color:var(--warn);min-height:1.2em;font-size:.9rem;margin-top:8px}
</style></head><body>
<h1>SPACE CARROTS &middot; TEST STAND</h1>

<div class="card">
  <div class="state" id="state">--</div>
  <div class="big"><span id="thrust">0.00</span> <span class="unit">N</span></div>
  <canvas id="plot" width="480" height="110"></canvas>
</div>

<div class="card">
  <div class="row"><span>Timer</span><span class="v" id="timer">--</span></div>
  <div class="row"><span>Continuity</span><span class="v" id="cont">--</span></div>
  <div class="row"><span>RBF key</span><span class="v" id="rbf">--</span></div>
  <div class="row"><span>Pyro channel</span><span class="v" id="pyro">--</span></div>
  <div class="row"><span>Load cell</span><span class="v" id="hx">--</span></div>
  <div class="row"><span>Storage</span><span class="v" id="log">--</span></div>
  <div class="row"><span>Samples logged</span><span class="v" id="samples">0</span></div>
  <div class="row"><span>Burns stored</span><span class="v" id="burns">0</span></div>
  <div class="row"><span>Igniter on-time</span><span class="v" id="ignms">-- ms</span></div>
  <div class="row"><span>Last abort</span><span class="v" id="abort">none</span></div>
</div>

<div class="card">
  <input type="password" id="code" placeholder="CODE" autocomplete="off">
  <button class="fire" id="fireBtn" onclick="fire()">START COUNTDOWN</button>
  <button class="abort" onclick="post('/api/abort')">ABORT</button>
  <button class="tare" onclick="post('/api/tare')">TARE</button>
  <div class="msg" id="msg"></div>
</div>

<div class="card">
  <div class="row"><span>Igniter on-time [ms]</span><span></span></div>
  <input type="number" id="ignInput" min="10" max="5000" step="10" placeholder="300">
  <button class="tare" id="ignBtn" onclick="setIgnition()">SET (uses the CODE above)</button>
</div>

<script>
const S=["BOOT","IDLE","COUNTDOWN","RECORDING","FAULT","DUMPING"];
const hist=new Array(160).fill(0);
const cv=document.getElementById('plot'),cx=cv.getContext('2d');
// Auto-scales to whatever is actually on screen right now, min AND max
// (not just abs(max)), so it copes with negative baseline drift or noise
// instead of assuming thrust is always >= 0. A small minimum span keeps
// idle noise from being blown up into a huge trace.
function draw(){
 const w=cv.width,h=cv.height;cx.clearRect(0,0,w,h);
 let lo=0,hi=0;
 for(const v of hist){if(v<lo)lo=v;if(v>hi)hi=v;}
 if(hi-lo<1){const mid=(hi+lo)/2;lo=mid-0.5;hi=mid+0.5;}
 const span=hi-lo,pad=span*0.08;lo-=pad;hi+=pad;
 const top=8,bot=h-4,rng=bot-top;
 const yOf=v=>bot-((v-lo)/(hi-lo))*rng;
 cx.strokeStyle='#2c2c2a';cx.lineWidth=1;
 cx.beginPath();cx.moveTo(0,yOf(0));cx.lineTo(w,yOf(0));cx.stroke();
 cx.strokeStyle='#3987e5';cx.lineWidth=2;cx.beginPath();
 hist.forEach((v,i)=>{const x=i/(hist.length-1)*w,y=yOf(v);
   i?cx.lineTo(x,y):cx.moveTo(x,y);});
 cx.stroke();
 cx.fillStyle='#898781';cx.font='11px system-ui';
 cx.fillText(hi.toFixed(1)+' N',4,12);
 if(lo<-0.05)cx.fillText(lo.toFixed(1)+' N',4,h-6);
}
function cls(el,c){el.className='v '+c;}
async function tick(){
 try{
  const d=await (await fetch('/api/data')).json();
  document.getElementById('thrust').textContent=(d.thrust/1000).toFixed(2);
  hist.push(d.thrust/1000);hist.shift();draw();
  const st=document.getElementById('state');
  st.textContent=S[d.state]||'?';
  st.style.color=d.state==2?'#fab219':(d.state==3?'#d03b3b':(d.state==4?'#d03b3b':'#0ca30c'));
  document.getElementById('timer').textContent =
    d.state==2 ? 'T-'+(d.t/1000).toFixed(1)+' s' :
    d.state==3 ? 'T+'+(d.t/1000).toFixed(1)+' s' : '--';
  const c=document.getElementById('cont');c.textContent=d.cont?'OK':'OPEN';cls(c,d.cont?'ok':'crit');
  const r=document.getElementById('rbf');r.textContent=d.rbf?'IN / SAFE':'OUT / ARMED';cls(r,d.rbf?'ok':'warn');
  const p=document.getElementById('pyro');p.textContent=d.pyro?'LIVE':'off';cls(p,d.pyro?'crit':'ok');
  const x=document.getElementById('hx');x.textContent=d.hx?'OK':'FAULT';cls(x,d.hx?'ok':'crit');
  const l=document.getElementById('log');
  l.textContent=d.log?(d.free+' slots free'):'DISABLED';cls(l,d.log?(d.free?'ok':'warn'):'crit');
  document.getElementById('samples').textContent=d.samples;
  document.getElementById('burns').textContent=d.burns;
  document.getElementById('abort').textContent=d.abort;
  document.getElementById('fireBtn').disabled=(d.state!=1)||d.rbf;
  document.getElementById('ignms').textContent=d.ign_ms+' ms';
  const ii=document.getElementById('ignInput');
  if(document.activeElement!==ii && !ii.value) ii.placeholder=d.ign_ms;
 }catch(e){document.getElementById('state').textContent='LINK LOST';}
}
function msg(t){document.getElementById('msg').textContent=t;
 setTimeout(()=>document.getElementById('msg').textContent='',4000);}
async function post(u,q=''){const r=await fetch(u+q,{method:'POST'});msg(await r.text());}
function fire(){
 const c=document.getElementById('code').value;
 if(!c){msg('enter the code');return;}
 if(!confirm('Start the countdown? Everyone clear of the stand?'))return;
 post('/api/fire','?code='+encodeURIComponent(c));
}
function setIgnition(){
 const c=document.getElementById('code').value;
 const ms=document.getElementById('ignInput').value;
 if(!c){msg('enter the code above first');return;}
 if(!ms){msg('enter the on-time in ms');return;}
 post('/api/set_ignition','?code='+encodeURIComponent(c)+'&ms='+encodeURIComponent(ms));
}
setInterval(tick,150);tick();
</script></body></html>
)rawliteral";

static void handleRoot() { server.send_P(200, "text/html", html_page); }

static void handleData() {
  char json[440];
  snprintf(json, sizeof(json),
           "{\"state\":%lu,\"thrust\":%ld,\"raw\":%ld,\"cont\":%d,\"rbf\":%d,\"pyro\":%d,"
           "\"hx\":%d,\"log\":%d,\"free\":%lu,\"t\":%ld,\"samples\":%lu,\"burns\":%lu,"
           "\"boots\":%lu,\"resumes\":%lu,\"abort\":\"%s\",\"ign_ms\":%lu}",
           (unsigned long)tlm_state, (long)tlm_thrust_mN, (long)tlm_raw,
           tlm_cont ? 1 : 0, tlm_rbf ? 1 : 0, tlm_pyro ? 1 : 0,
           tlm_hx_ok ? 1 : 0, tlm_log_ok ? 1 : 0, (unsigned long)tlm_free_slots,
           (long)tlm_t_ms, (unsigned long)tlm_samples, (unsigned long)tlm_total_burns,
           (unsigned long)tlm_boot_count, (unsigned long)tlm_resume_count,
           abortName(tlm_last_abort), (unsigned long)tlm_ignition_ms);
  server.send(200, "application/json", json);
}

// Constant time compare so the code cannot be guessed byte by byte.
static bool codeMatches(const String &given) {
  const char *want = SECRET_CODE;
  size_t wl = strlen(want);
  uint8_t diff = (uint8_t)(given.length() ^ wl);
  for (size_t i = 0; i < wl; i++)
    diff |= (uint8_t)(want[i] ^ (i < given.length() ? given[i] : 0));
  return diff == 0;
}

static void handleFire() {
  uint32_t now = millis();
  if (web_lock_ms && (int32_t)(now - web_lock_ms) < 0) {
    server.send(429, "text/plain", "locked out, wait a minute");
    return;
  }
  if (!server.hasArg("code") || !codeMatches(server.arg("code"))) {
    if (++web_fails >= MAX_FAILED_CODES) { web_lock_ms = now + LOCKOUT_MS; web_fails = 0; }
    server.send(403, "text/plain", "wrong code");
    return;
  }
  web_fails = 0;
  if (tlm_state != ST_IDLE) { server.send(409, "text/plain", "not idle"); return; }
  if (tlm_rbf)              { server.send(409, "text/plain", "remove the RBF key"); return; }
  if (!tlm_log_ok)          { server.send(409, "text/plain", "storage fault"); return; }
  if (!tlm_hx_ok)           { server.send(409, "text/plain", "load cell fault"); return; }
#if REQUIRE_CONTINUITY_TO_ARM
  // Mirrors interlocksOk() on core 0, which silently refuses to start the
  // countdown without continuity. Without this check the web UI would
  // report "countdown started" even though nothing actually happens.
  if (!tlm_cont)            { server.send(409, "text/plain", "no igniter continuity"); return; }
#endif
  req_fire = 1;
  server.send(200, "text/plain", "countdown started");
}

static void handleAbort() {
  req_abort = 1;
  server.send(200, "text/plain", "abort sent");
}

static void handleTare() {
  if (tlm_state != ST_IDLE) { server.send(409, "text/plain", "not idle"); return; }
  req_tare = 1;
  server.send(200, "text/plain", "tare requested");
}

static void handleSetIgnition() {
  uint32_t now = millis();
  if (web_lock_ms && (int32_t)(now - web_lock_ms) < 0) {
    server.send(429, "text/plain", "locked out, wait a minute");
    return;
  }
  if (!server.hasArg("code") || !codeMatches(server.arg("code"))) {
    if (++web_fails >= MAX_FAILED_CODES) { web_lock_ms = now + LOCKOUT_MS; web_fails = 0; }
    server.send(403, "text/plain", "wrong code");
    return;
  }
  web_fails = 0;
  if (tlm_state != ST_IDLE) { server.send(409, "text/plain", "not idle"); return; }
  if (!server.hasArg("ms"))  { server.send(400, "text/plain", "missing ms"); return; }
  long ms = server.arg("ms").toInt();
  if (ms < 10 || ms > (long)IGNITION_MAX_MS) {
    server.send(400, "text/plain", "must be 10 - " + String(IGNITION_MAX_MS) + " ms");
    return;
  }
  req_ignition_ms  = (uint32_t)ms;
  req_set_ignition = 1;
  server.send(200, "text/plain", "igniter on-time updated");
}

void setup1() {
  WiFi.mode(WIFI_AP);
  WiFi.softAP(AP_SSID, AP_PASSWORD);
  server.on("/", HTTP_GET, handleRoot);
  server.on("/api/data",         HTTP_GET,  handleData);
  server.on("/api/fire",         HTTP_POST, handleFire);
  server.on("/api/abort",        HTTP_POST, handleAbort);
  server.on("/api/tare",         HTTP_POST, handleTare);
  server.on("/api/set_ignition", HTTP_POST, handleSetIgnition);
  server.onNotFound([]() { server.send(404, "text/plain", "no"); });
  server.begin();
}

void loop1() {
  server.handleClient();
  // Core 1 is parked by core 0 during flash writes; nothing here may
  // hold a lock that core 0 needs.
  delay(1);
}
