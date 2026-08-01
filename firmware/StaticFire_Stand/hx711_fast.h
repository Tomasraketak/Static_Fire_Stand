// =====================================================================
//  Minimal, dependency free HX711 driver.
//
//  Why not the stock HX711 library:
//    * get_units() blocks for an unbounded time if the sensor dies
//    * no way to timestamp the conversion, which is exactly what we need
//      to measure ignition delay to the millisecond
//    * it hides read errors instead of reporting them
//
//  This driver never blocks: you poll ready() and call readRaw() only
//  when a conversion is actually waiting.
// =====================================================================
#pragma once
#include <Arduino.h>
#include <hardware/sync.h>

class HX711Fast {
public:
  void begin(uint8_t pinDout, uint8_t pinSck, uint8_t gain = 128) {
    _dout = pinDout;
    _sck  = pinSck;
    setGain(gain);
    pinMode(_sck, OUTPUT);
    pinMode(_dout, INPUT_PULLUP);
    digitalWrite(_sck, LOW);
    _lastReady = micros();
  }

  void setGain(uint8_t gain) {
    switch (gain) {
      case 64:  _extraPulses = 3; break;   // channel A, gain 64
      case 32:  _extraPulses = 2; break;   // channel B, gain 32
      default:  _extraPulses = 1; break;   // channel A, gain 128
    }
  }

  // DOUT goes low when a conversion is ready.
  bool ready() {
    bool r = (digitalRead(_dout) == LOW);
    if (r) _lastReady = micros();
    return r;
  }

  // True when the chip has not produced a sample for a long time.
  bool faulted(uint32_t timeoutUs) const {
    return (uint32_t)(micros() - _lastReady) > timeoutUs;
  }

  // Clock out 24 bits. Interrupts are masked because holding PD_SCK high
  // for more than 60 us puts the HX711 into power down.
  int32_t readRaw() {
    uint32_t value = 0;
    uint32_t save = save_and_disable_interrupts();
    for (uint8_t i = 0; i < 24; i++) {
      digitalWrite(_sck, HIGH);
      delayMicroseconds(1);
      value = (value << 1) | (digitalRead(_dout) == HIGH ? 1u : 0u);
      digitalWrite(_sck, LOW);
      delayMicroseconds(1);
    }
    for (uint8_t i = 0; i < _extraPulses; i++) {
      digitalWrite(_sck, HIGH);
      delayMicroseconds(1);
      digitalWrite(_sck, LOW);
      delayMicroseconds(1);
    }
    restore_interrupts(save);

    // sign extend the 24 bit two's complement result
    if (value & 0x800000u) value |= 0xFF000000u;
    _lastReady = micros();
    return (int32_t)value;
  }

  // Blocking read with a bounded wait. Returns false on timeout.
  bool readBlocking(int32_t &out, uint32_t timeoutUs = 400000UL) {
    uint32_t t0 = micros();
    while (!ready()) {
      if ((uint32_t)(micros() - t0) > timeoutUs) return false;
      tight_loop_contents();
    }
    out = readRaw();
    return true;
  }

  // Median-of-N tare. Median rejects the odd spike far better than a mean.
  bool tare(uint8_t samples, int32_t &offsetOut) {
    static int32_t buf[64];
    if (samples > 64) samples = 64;
    uint8_t got = 0;
    for (uint8_t i = 0; i < samples; i++) {
      int32_t v;
      if (readBlocking(v)) buf[got++] = v;
    }
    if (got < 3) return false;
    for (uint8_t i = 1; i < got; i++) {           // insertion sort, N is tiny
      int32_t k = buf[i];
      int8_t j = i - 1;
      while (j >= 0 && buf[j] > k) { buf[j + 1] = buf[j]; j--; }
      buf[j + 1] = k;
    }
    offsetOut = buf[got / 2];
    return true;
  }

  void powerDown() {
    digitalWrite(_sck, LOW);
    digitalWrite(_sck, HIGH);
    delayMicroseconds(70);
  }

  void powerUp() {
    digitalWrite(_sck, LOW);
    delayMicroseconds(70);
  }

private:
  uint8_t  _dout = 0, _sck = 0, _extraPulses = 1;
  volatile uint32_t _lastReady = 0;
};
