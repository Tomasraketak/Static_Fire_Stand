// =====================================================================
//  Arduino / arduino-pico shim for the stand firmware - HOST SYNTAX CHECK
//  ONLY (`make syntax`). Nothing here does anything; it exists so a typo,
//  a wrong type or a bad printf format shows up on a workstation. It does
//  NOT replace building with arduino-cli before flashing.
// =====================================================================
#pragma once
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <stdlib.h>
#include <stdio.h>
#include <math.h>
#include <string>

#define HIGH 1
#define LOW 0
#define INPUT 0
#define OUTPUT 1
#define INPUT_PULLUP 2
#define INPUT_PULLDOWN 3
#define PROGMEM
#define F(x) x
#define XIP_BASE 0x10000000u
#define NEO_GRB 0x52
#define NEO_KHZ800 0

inline void pinMode(uint8_t, uint8_t) {}
inline void digitalWrite(uint8_t, int) {}
inline int  digitalRead(uint8_t) { return 0; }
inline int  analogRead(uint8_t) { return 0; }
inline void analogReadResolution(int) {}
inline uint32_t millis(void) { return 0; }
inline uint32_t micros(void) { return 0; }
inline void delay(uint32_t) {}
inline void delayMicroseconds(uint32_t) {}
inline void tight_loop_contents(void) {}

class String {
public:
    String() {}
    String(const char *s) : s_(s) {}
    String(unsigned long v) : s_(std::to_string(v)) {}
    String(unsigned v) : s_(std::to_string(v)) {}
    String(int v) : s_(std::to_string(v)) {}
    void trim() {}
    size_t length() const { return s_.size(); }
    bool startsWith(const char *p) const { return s_.rfind(p, 0) == 0; }
    String substring(unsigned from) const { return String(s_.substr(from).c_str()); }
    float toFloat() const { return (float)atof(s_.c_str()); }
    long toInt() const { return atol(s_.c_str()); }
    char operator[](size_t i) const { return s_[i]; }
    bool operator==(const char *o) const { return s_ == o; }
    String operator+(const char *o) const { return String((s_ + o).c_str()); }
    String operator+(const String &o) const { return String((s_ + o.s_).c_str()); }
    friend String operator+(const char *a, const String &b) { return String((std::string(a) + b.s_).c_str()); }
private:
    std::string s_;
};

class SerialStub {
public:
    void begin(uint32_t) {}
    int available() { return 0; }
    int read() { return -1; }
    String readStringUntil(char) { return String(); }
    size_t write(const uint8_t *, size_t) { return 0; }
    size_t print(const char *) { return 0; }
    size_t println(const char *) { return 0; }
    size_t println(void) { return 0; }
    void flush() {}
    __attribute__((format(printf, 2, 3)))
    size_t printf(const char *, ...) { return 0; }
};
extern SerialStub Serial;

struct Rp2040Stub {
    void idleOtherCore() {}
    void resumeOtherCore() {}
    void wdt_begin(uint32_t) {}
    void wdt_reset() {}
    void reboot() {}
};
extern Rp2040Stub rp2040;

template <class T> inline T min(T a, T b) { return a < b ? a : b; }
