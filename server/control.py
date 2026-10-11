"""Shared limits for the web forms and device configuration protocol."""
DEFAULT_SETTINGS = {
    "enabled": True, "start": 29400, "end": 31259,
    "reboot_enabled": True, "reboot_at": 14400,
    "home": 1500, "repeats": 2,
    "steps": [[1500, 200], [700, 200], [1500, 500], [700, 200],
              [1500, 50], [700, 200], [1500, 2000], [700, 200]],
}
KINDS = {"punch": "完整打卡", "test": "测试动作（不计打卡）", "position": "舵机定位", "reboot": "设备重启"}


def integer(value, low, high):
    return type(value) is int and low <= value <= high


def valid_settings(s):
    if not isinstance(s, dict):
        return False
    if not (type(s.get("enabled")) is bool and type(s.get("reboot_enabled")) is bool
            and integer(s.get("start"), 0, 86399) and integer(s.get("end"), s["start"], 86399)
            and integer(s.get("reboot_at"), 0, 86399) and integer(s.get("home"), 400, 2500)
            and integer(s.get("repeats"), 1, 5) and isinstance(s.get("steps"), list)
            and 1 <= len(s["steps"]) <= 16):
        return False
    for step in s["steps"]:
        if not (isinstance(step, list) and len(step) == 2
                and integer(step[0], 400, 2500) and integer(step[1], 20, 5000)):
            return False
    duration = s["repeats"] * (sum(step[1] for step in s["steps"]) + 600) + 500
    return duration <= 20000 and not (s["enabled"] and s["reboot_enabled"] and s["start"] <= s["reboot_at"] <= s["end"])


def form_settings(form):
    def seconds(key):
        hour, minute = map(int, form[key].split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("invalid time")
        return hour * 3600 + minute * 60
    s = dict(enabled="enabled" in form, start=seconds("start"), end=seconds("end") + 59,
             reboot_enabled="reboot_enabled" in form, reboot_at=seconds("reboot_at"),
             home=int(form["home"]), repeats=int(form["repeats"]),
             steps=[list(map(int, line.split(","))) for line in form["steps"].splitlines() if line.strip()])
    if not valid_settings(s):
        raise ValueError("invalid settings")
    return s
