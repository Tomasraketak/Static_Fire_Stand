#pragma once
#include <Arduino.h>
#define WIFI_AP 2
class WiFiClass { public: void mode(int) {} bool softAP(const char *, const char *) { return true; } };
extern WiFiClass WiFi;
