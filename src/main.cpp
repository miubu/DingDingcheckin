#include <Arduino.h>
#include <ArduinoJson.h>
#include <ESP32Servo.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <WiFi.h>
#include <time.h>
#include "config.h"

Servo servo;
Preferences prefs;
JsonDocument state;
bool storageOK = false;
unsigned long lastNetwork = 0, lastReconnect = 0, lastCheck = 0;

bool saveState() {
    String json;
    serializeJson(state, json);
    storageOK = prefs.putString("state", json) == json.length();
    if (!storageOK) Serial.println("Storage error: motion disabled; restart after fixing NVS");
    return storageOK;
}

bool clockReady() { return time(nullptr) >= 1704067200; }

int todayNumber() {
    time_t epoch = time(nullptr);
    tm t;
    localtime_r(&epoch, &t);
    return (t.tm_year + 1900) * 10000 + (t.tm_mon + 1) * 100 + t.tm_mday;
}

bool scheduledNow() {
    if (!clockReady()) return false;
    time_t epoch = time(nullptr);
    tm t;
    localtime_r(&epoch, &t);
    int seconds = t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec;
    return seconds >= CHECKIN_START && seconds <= CHECKIN_END &&
           state["date"].as<int>() != todayNumber();
}

String eventId() {
    char id[33];
    snprintf(id, sizeof(id), "%08lx%08lx%08lx%08lx",
             (unsigned long)esp_random(), (unsigned long)esp_random(),
             (unsigned long)esp_random(), (unsigned long)esp_random());
    return String(id);
}

bool runServo() {
    servo.setPeriodHertz(50);
    servo.attach(SERVO_PIN, SERVO_MIN_US, SERVO_MAX_US);
    if (!servo.attached()) return false;
    // Preserve the original calibrated pulses, timing and two repetitions.
    for (int repeat = 0; repeat < SERVO_REPEATS; ++repeat) {
        for (size_t i = 0; i < sizeof(PULSES) / sizeof(PULSES[0]); ++i) {
            servo.writeMicroseconds(PULSES[i]);
            delay(DELAYS[i]);
        }
        servo.writeMicroseconds(SERVO_HOME_US);
        delay(600);
    }
    servo.writeMicroseconds(SERVO_HOME_US);
    delay(500);
    servo.detach();
    pinMode(SERVO_PIN, OUTPUT);
    digitalWrite(SERVO_PIN, LOW);
    return true;  // PWM sequence finished, not a DingTalk attendance confirmation.
}

void executePunch(const String& command = "") {
    if (!storageOK || !clockReady()) return;
    if (command.length() && command == state["command"].as<String>()) return;
    JsonArray events = state["events"].as<JsonArray>();
    // ponytail: 16 durable events; stop motion when full. Use a larger journal for long outages.
    if (events.size() >= 16) { Serial.println("Outbox full; restore server connection"); return; }
    JsonObject event = events.add<JsonObject>();
    event["id"] = eventId();
    event["at"] = (int64_t)time(nullptr);
    event["source"] = command.length() ? "manual" : "automatic";
    event["command"] = command;
    event["status"] = "interrupted";
    state["date"] = todayNumber();
    if (command.length()) state["command"] = command;
    // Commit the claim BEFORE any physical motion. A reset must never replay it.
    if (!saveState()) return;
    event["status"] = runServo() ? "completed" : "failed";
    saveState();
    Serial.println("Motion ended; result queued");
}

int post(const char* path, JsonDocument& data, String& response) {
    WiFiClient client;
    HTTPClient http;
    http.setConnectTimeout(2000);
    http.setTimeout(3000);
    if (!http.begin(client, String(SERVER_URL) + path)) return -1;
    http.addHeader("Authorization", String("Bearer ") + DEVICE_TOKEN);
    http.addHeader("Content-Type", "application/json");
    String body;
    serializeJson(data, body);
    int code = http.POST(body);
    response = http.getString();
    http.end();
    if (code < 200 || code >= 300) Serial.printf("Server HTTP result: %d\n", code);
    return code;
}

void networkTick() {
    if (WiFi.status() != WL_CONNECTED) return;
    String response;
    JsonArray events = state["events"].as<JsonArray>();
    // At most one event per poll keeps the local scheduler responsive during an outage.
    if (storageOK && events.size()) {
        JsonDocument payload;
        payload["device"] = DEVICE_ID;
        payload["event"].set(events[0]);
        int code = post("/api/events", payload, response);
        JsonDocument ack;
        if (code == 200 && !deserializeJson(ack, response) && ack["ok"] == true &&
            ack["id"].as<String>() == events[0]["id"].as<String>()) {
            events.remove(0);
            if (!saveState()) return;
        }
    }
    JsonDocument heartbeat;
    heartbeat["device"] = DEVICE_ID;
    heartbeat["time_valid"] = clockReady();
    heartbeat["storage_ok"] = storageOK;
    heartbeat["pending"] = events.size();
    heartbeat["rssi"] = WiFi.RSSI();
    heartbeat["temperature"] = temperatureRead();
    heartbeat["start"] = CHECKIN_START;
    heartbeat["end"] = CHECKIN_END;
    heartbeat["last_date"] = state["date"] | 0;
    if (post("/api/poll", heartbeat, response) != 200) return;
    JsonDocument reply;
    if (deserializeJson(reply, response)) return;
    String command = reply["command"]["id"] | "";
    int64_t expires = reply["command"]["expires"] | (int64_t)0;
    if (command.length() && clockReady() && (int64_t)time(nullptr) < expires) {
        // Drain older results before accepting another command.
        if (storageOK && events.size() == 0) executePunch(command);
    }
}

void setup() {
    Serial.begin(115200);
    pinMode(SERVO_PIN, OUTPUT);
    digitalWrite(SERVO_PIN, LOW);
    if (prefs.begin("checkin", false)) {
        String saved = prefs.getString("state", "");
        if (saved.isEmpty()) {
            state["date"] = prefs.getInt("date", 0);  // Migrate the original daily lock.
            state["command"] = "";
            state["events"].to<JsonArray>();
            saveState();
        } else if (!deserializeJson(state, saved) && state["date"].is<int>() &&
                   state["command"].is<String>() && state["events"].is<JsonArray>()) {
            storageOK = true;
        } else {
            Serial.println("Invalid saved state; motion disabled, do not erase before reviewing");
        }
    }
    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(true);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    configTzTime("CST-8", "202.118.66.180", "time.dlut.edu.cn", "ntp.aliyun.com");
    Serial.println("Schedule: daily 08:10-08:40; WiFi stays on for manual commands");
}

void loop() {
    unsigned long now = millis();
    if (now - lastCheck >= 1000) {
        lastCheck = now;
        if (storageOK && scheduledNow()) executePunch();
    }
    if (WiFi.status() != WL_CONNECTED && now - lastReconnect >= 30000) {
        lastReconnect = now;
        WiFi.reconnect();
    }
    if (now - lastNetwork >= POLL_MS) {
        lastNetwork = now;
        networkTick();
    }
    delay(20);
}
