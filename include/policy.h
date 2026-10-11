#pragma once
inline bool punchDue(bool validClock, bool enabled, int seconds, int start, int end, int date, int lastDate) {
    return validClock && enabled && seconds >= start && seconds <= end && date != lastDate;
}
inline bool rebootDue(bool validClock, bool enabled, int seconds, int at, int date, int lastDate,
                      unsigned long uptime, bool inPunchWindow) {
    // A late boot may restart once, but never loop or interrupt the check-in window.
    return validClock && enabled && seconds >= at && date != lastDate && uptime >= 3600000UL && !inPunchWindow;
}
