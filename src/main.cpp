#include <Arduino.h>
#include <ArduinoJson.h>
#include <ESP32Servo.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <WiFi.h>
#include <esp_system.h>
#include <sys/time.h>
#include <time.h>
#include <vector>
#include "config.h"
#include "policy.h"

Servo servo;
Preferences prefs;
JsonDocument state;
bool storageOK = false, prefsOK = false;
int lastHttp = 0;
unsigned long lastNetwork = 0, lastReconnect = 0, lastCheck = 0, lastStorageRetry = 0;

bool saveState() {
    String json;
    serializeJson(state, json);
    // NVS strings are limited to 4000 bytes; a blob accommodates the durable outbox.
    storageOK = prefsOK && json.length() <= 16384 && prefs.putBytes("state_v2", json.c_str(), json.length()) == json.length();
    if (!storageOK) Serial.println("Storage error: motion disabled; retrying in 30 seconds");
    return storageOK;
}
bool clockReady() { return time(nullptr) >= 1704067200; }
tm localClock() {
    time_t epoch = time(nullptr);
    tm t{};
    localtime_r(&epoch, &t);
    return t;
}
int todayNumber() {
    tm t = localClock();
    return (t.tm_year + 1900) * 10000 + (t.tm_mon + 1) * 100 + t.tm_mday;
}
int daySeconds() {
    tm t = localClock();
    return t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec;
}
bool validSettings(JsonVariantConst s) {
    if (!s.is<JsonObjectConst>() || !s["enabled"].is<bool>() || !s["reboot_enabled"].is<bool>() ||
        !s["start"].is<int>() || !s["end"].is<int>() || !s["reboot_at"].is<int>() ||
        !s["home"].is<int>() || !s["repeats"].is<int>() || !s["steps"].is<JsonArrayConst>()) return false;
    int start = s["start"], end = s["end"], reboot = s["reboot_at"], home = s["home"], repeats = s["repeats"];
    JsonArrayConst steps = s["steps"];
    if (start < 0 || end < start || end > 86399 || reboot < 0 || reboot > 86399 ||
        home < SERVO_MIN_US || home > SERVO_MAX_US || repeats < 1 || repeats > 5 ||
        steps.size() < 1 || steps.size() > 16) return false;
    int duration = 600;
    for (JsonVariantConst step : steps) {
        if (!step.is<JsonArrayConst>() || step.size() != 2 || !step[0].is<int>() || !step[1].is<int>()) return false;
        int pulse = step[0], wait = step[1];
        if (pulse < SERVO_MIN_US || pulse > SERVO_MAX_US || wait < 20 || wait > 5000) return false;
        duration += wait;
    }
    return duration * repeats + 500 <= 20000 &&
           !(s["enabled"].as<bool>() && s["reboot_enabled"].as<bool>() && reboot >= start && reboot <= end);
}
void defaultSettings() {
    JsonObject s = state["settings"].to<JsonObject>();
    s["enabled"] = true;
    s["start"] = CHECKIN_START;
    s["end"] = CHECKIN_END;
    s["reboot_enabled"] = true;
    s["reboot_at"] = 4 * 3600;
    s["home"] = SERVO_HOME_US;
    s["repeats"] = SERVO_REPEATS;
    JsonArray steps = s["steps"].to<JsonArray>();
    for (size_t i = 0; i < sizeof(PULSES) / sizeof(PULSES[0]); ++i) {
        JsonArray step = steps.add<JsonArray>();
        step.add(PULSES[i]);
        step.add(DELAYS[i]);
    }
    state["revision"] = 0;
}
String eventId() {
    char id[33];
    snprintf(id, sizeof(id), "%08lx%08lx%08lx%08lx",
             (unsigned long)esp_random(), (unsigned long)esp_random(),
             (unsigned long)esp_random(), (unsigned long)esp_random());
    return String(id);
}
void releaseServo() {
    servo.detach();
    pinMode(SERVO_PIN, OUTPUT);
    digitalWrite(SERVO_PIN, LOW);
}
bool runServo(const String& kind, JsonVariantConst payload) {
    JsonVariantConst s = payload["settings"];
    if (kind == "position") {
        if (!payload["pulse"].is<int>() || !payload["hold"].is<int>()) return false;
        int pulse = payload["pulse"], hold = payload["hold"];
        if (pulse < SERVO_MIN_US || pulse > SERVO_MAX_US || hold < 100 || hold > 5000) return false;
    } else if ((kind != "punch" && kind != "test") || !validSettings(s)) return false;
    servo.setPeriodHertz(50);
    servo.attach(SERVO_PIN, SERVO_MIN_US, SERVO_MAX_US);
    if (!servo.attached()) { releaseServo(); return false; }
    // shortcut: bounded blocking motion (max 20 s); use a state machine if emergency stop is needed.
    if (kind == "position") {
        servo.writeMicroseconds(payload["pulse"].as<int>());
        delay(payload["hold"].as<int>());
    } else {
        for (int repeat = 0; repeat < s["repeats"].as<int>(); ++repeat) {
            for (JsonArrayConst step : s["steps"].as<JsonArrayConst>()) {
                servo.writeMicroseconds(step[0].as<int>());
                delay(step[1].as<int>());
            }
            servo.writeMicroseconds(s["home"].as<int>());
            delay(600);
        }
        servo.writeMicroseconds(s["home"].as<int>());
        delay(500);
    }
    releaseServo();
    return true;  // PWM completion is NOT a DingTalk attendance confirmation.
}
void execute(const String& kind, const String& command, JsonVariantConst payload) {
    if (!storageOK || !clockReady()) return;
    if (command.length() && command == state["command"].as<String>()) return;
    JsonArray events = state["events"].as<JsonArray>();
    // shortcut: preserve 16 offline results, then stop; enlarge the journal for longer outages.
    if (events.size() >= 16) { Serial.println("Outbox full: restore server connection"); return; }
    JsonObject event = events.add<JsonObject>();
    event["id"] = eventId();
    event["at"] = (int64_t)time(nullptr);
    event["source"] = command.length() ? "manual" : "automatic";
    event["command"] = command;
    event["kind"] = kind;
    event["status"] = "interrupted";
    if (kind == "punch") state["date"] = todayNumber();
    if (command.length()) state["command"] = command;
    // Claim before motion: power failure must never silently replay a physical action.
    if (!saveState()) return;
    if (kind == "reboot") {
        state["reboot_date"] = todayNumber();
        event["status"] = "completed";
        if (!saveState()) return;
        Serial.println("Manual reboot: acknowledgement will upload after boot");
        delay(100);
        ESP.restart();
        return;
    }
    event["status"] = runServo(kind, payload) ? "completed" : "failed";
    saveState();
    Serial.println("Motion ended; result queued");
}
int post(const char* path, JsonDocument& data, String& response) {
    WiFiClient client;
    HTTPClient http;
    http.setConnectTimeout(2000);
    http.setTimeout(3000);
    if (!http.begin(client, String(SERVER_URL) + path)) return lastHttp = -1;
    http.addHeader("Authorization", String("Bearer ") + DEVICE_TOKEN);
    http.addHeader("Content-Type", "application/json");
    String body;
    serializeJson(data, body);
    lastHttp = http.POST(body);
    response = http.getSize() > 16384 ? "" : http.getString();
    http.end();
    if (lastHttp < 200 || lastHttp >= 300) Serial.printf("Server HTTP result: %d\n", lastHttp);
    return lastHttp;
}
void networkTick() {
    if (WiFi.status() != WL_CONNECTED) return;
    String response;
    JsonArray events = state["events"].as<JsonArray>();
    if (storageOK && events.size()) {
        JsonDocument payload;
        payload["device"] = DEVICE_ID;
        payload["event"].set(events[0]);
        JsonDocument ack;
        if (post("/api/events", payload, response) == 200 && !deserializeJson(ack, response) &&
            ack["ok"] == true && ack["id"].as<String>() == events[0]["id"].as<String>()) {
            events.remove(0);
            saveState();
        }
    }
    JsonDocument heartbeat;
    heartbeat["device"] = DEVICE_ID;
    heartbeat["protocol"] = 2;
    heartbeat["time_valid"] = clockReady();
    heartbeat["storage_ok"] = storageOK;
    heartbeat["pending"] = events.size();
    heartbeat["rssi"] = WiFi.RSSI();
    heartbeat["temperature"] = temperatureRead();
    heartbeat["start"] = state["settings"]["start"] | CHECKIN_START;
    heartbeat["end"] = state["settings"]["end"] | CHECKIN_END;
    heartbeat["last_date"] = state["date"] | 0;
    heartbeat["revision"] = state["revision"] | 0;
    heartbeat["uptime"] = millis() / 1000UL;
    heartbeat["heap"] = ESP.getFreeHeap();
    heartbeat["reset_reason"] = (int)esp_reset_reason();
    heartbeat["last_http"] = lastHttp;
    if (post("/api/poll", heartbeat, response) != 200) return;
    JsonDocument reply;
    if (deserializeJson(reply, response)) return;
    int64_t serverTime = reply["server_time"] | (int64_t)0;
    // The authenticated server is a fallback when public NTP is unreachable.
    if (!clockReady() && serverTime >= 1704067200 && serverTime <= 4102444800LL) {
        timeval tv{(time_t)serverTime, 0};
        settimeofday(&tv, nullptr);
    }
    int revision = reply["revision"] | 0;
    if (storageOK && revision > 0 && revision != (state["revision"] | 0) && validSettings(reply["settings"])) {
        state["settings"].set(reply["settings"]);
        state["revision"] = revision;
        saveState();
    }
    String command = reply["command"]["id"] | "";
    String kind = reply["command"]["kind"] | "punch";
    int64_t expires = reply["command"]["expires"] | (int64_t)0;
    if (command.length() == 32 && clockReady() && (int64_t)time(nullptr) < expires &&
        storageOK && events.size() == 0 && (kind == "punch" || kind == "test" || kind == "position" || kind == "reboot")) {
        if (kind == "punch" && reply["command"]["payload"].isNull()) {
            JsonDocument payload;  // Allow firmware-first upgrades against the original server.
            payload["settings"].set(state["settings"]);
            execute(kind, command, payload.as<JsonVariantConst>());
        } else execute(kind, command, reply["command"]["payload"]);
    }
}
void setup() {
    Serial.begin(115200);
    pinMode(SERVO_PIN, OUTPUT);
    digitalWrite(SERVO_PIN, LOW);
    prefsOK = prefs.begin("checkin", false);
    if (prefsOK) {
        String saved;
        size_t length = prefs.getBytesLength("state_v2");
        if (length && length <= 16384) {
            std::vector<char> buffer(length + 1, 0);
            if (prefs.getBytes("state_v2", buffer.data(), length) == length) saved = buffer.data();
        } else if (!length) saved = prefs.getString("state", "");
        bool valid = !length && saved.isEmpty();
        if (valid) {
            state["date"] = prefs.getInt("date", 0);
            state["command"] = "";
            state["events"].to<JsonArray>();
        } else valid = !saved.isEmpty() && !deserializeJson(state, saved) && state["date"].is<int>() &&
                      state["command"].is<String>() && state["events"].is<JsonArray>() && state["events"].size() <= 16;
        if (valid) {
            if (state["settings"].isNull()) defaultSettings();
            if (validSettings(state["settings"])) {
                if (saveState()) prefs.remove("state");
            } else {
                Serial.println("Invalid cached settings: motion disabled");
                prefsOK = false;
            }
        } else {
            Serial.println("Invalid saved state: motion disabled; review before erasing");
            prefsOK = false;  // Do not overwrite a corrupt journal during the retry loop.
        }
    }
    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);
    WiFi.setAutoReconnect(true);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    configTzTime("CST-8", "ntp.aliyun.com", "time.cloudflare.com", "202.118.66.180");
    Serial.printf("WiFi stays online; reset reason=%d\n", (int)esp_reset_reason());
}
void loop() {
    unsigned long now = millis();
    if (!storageOK && prefsOK && now - lastStorageRetry >= 30000) {
        lastStorageRetry = now;
        saveState();
    }
    if (now - lastCheck >= 1000) {
        lastCheck = now;
        JsonVariantConst s = state["settings"];
        int seconds = daySeconds(), date = todayNumber();
        bool inWindow = s["enabled"].as<bool>() && seconds >= s["start"].as<int>() && seconds <= s["end"].as<int>();
        if (storageOK && punchDue(clockReady(), s["enabled"], seconds, s["start"], s["end"], date, state["date"] | 0)) {
            JsonDocument payload;
            payload["settings"].set(s);
            execute("punch", "", payload.as<JsonVariantConst>());
        }
        if (storageOK && rebootDue(clockReady(), s["reboot_enabled"], seconds, s["reboot_at"], date,
                                   state["reboot_date"] | 0, now, inWindow)) {
            int previous = state["reboot_date"] | 0;
            state["reboot_date"] = date;
            if (saveState()) { Serial.println("Daily scheduled reboot"); delay(100); ESP.restart(); }
            state["reboot_date"] = previous;
        }
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
