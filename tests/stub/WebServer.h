#pragma once
#include <Arduino.h>
#include <functional>
enum HTTPMethod { HTTP_GET, HTTP_POST };
class WebServer {
public:
    WebServer(int) {}
    void on(const char *, HTTPMethod, std::function<void()>) {}
    void onNotFound(std::function<void()>) {}
    void begin() {}
    void handleClient() {}
    void send(int, const char *, const char *) {}
    void send(int, const char *, const String &) {}
    void send_P(int, const char *, const char *) {}
    bool hasArg(const char *) { return false; }
    String arg(const char *) { return String(); }
};
