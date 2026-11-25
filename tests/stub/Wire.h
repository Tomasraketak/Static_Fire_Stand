#pragma once
#include <Arduino.h>
class TwoWire {
public:
    void setSDA(uint8_t) {}
    void setSCL(uint8_t) {}
    void setTimeout(uint32_t, bool = false) {}
    void onReceive(void (*)(int)) {}
    void onRequest(void (*)(void)) {}
    void begin(void) {}
    void begin(uint8_t) {}
    int  available(void) { return 0; }
    int  read(void) { return -1; }
    size_t write(uint8_t) { return 1; }
};
extern TwoWire Wire;
