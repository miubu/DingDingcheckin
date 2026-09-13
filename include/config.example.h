#pragma once

constexpr char WIFI_SSID[] = "YOUR_WIFI_NAME";
constexpr char WIFI_PASS[] = "YOUR_WIFI_PASSWORD";
constexpr char SERVER_URL[] = "http://192.168.1.100:8888";
constexpr char DEVICE_TOKEN[] = "PASTE_THE_SAME_TOKEN_AS_SERVER_ENV";
constexpr char DEVICE_ID[] = "desk-esp32";

constexpr int CHECKIN_START = 8 * 3600 + 10 * 60;
constexpr int CHECKIN_END = 8 * 3600 + 40 * 60 + 59;
constexpr unsigned long POLL_MS = 5000;
constexpr int SERVO_PIN = 4;
constexpr int SERVO_MIN_US = 400, SERVO_MAX_US = 2500, SERVO_HOME_US = 1500;
constexpr int SERVO_REPEATS = 2;
constexpr int PULSES[] = {1500, 700, 1500, 700, 1500, 700, 1500, 700};
constexpr int DELAYS[] = {200, 200, 500, 200, 50, 200, 2000, 200};
static_assert(sizeof(PULSES) / sizeof(PULSES[0]) == sizeof(DELAYS) / sizeof(DELAYS[0]), "Pulse timing mismatch");
static_assert(0 <= CHECKIN_START && CHECKIN_START <= CHECKIN_END && CHECKIN_END < 86400, "Invalid daily window");
