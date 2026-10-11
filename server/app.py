import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for
from control import DEFAULT_SETTINGS, KINDS, form_settings, integer

LOCAL_TZ = timezone(timedelta(hours=8))
LABELS = {"completed": "动作完成", "failed": "动作失败", "interrupted": "动作中断／结果不确定",
          "pending": "等待设备领取", "sent": "已下发，等待结果", "expired": "已过期，未下发",
          "unknown": "结果未返回，请核实"}


def create_app(config=None):
    app = Flask(__name__)
    app.config.update(DEVICE_TOKEN=os.getenv("DEVICE_TOKEN", ""),
                      DEVICE_ID=os.getenv("DEVICE_ID", "desk-esp32"),
                      DATABASE=os.getenv("DATABASE", "data/checkin.db"),
                      MAX_CONTENT_LENGTH=16384)
    if config:
        app.config.update(config)
    if len(app.config["DEVICE_TOKEN"]) < 32:
        raise RuntimeError("DEVICE_TOKEN must be set to at least 32 characters; see .env.example")
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def db():
        connection = sqlite3.connect(app.config["DATABASE"], timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    with db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS device (
                id TEXT PRIMARY KEY, first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL, info TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS commands (
                id TEXT PRIMARY KEY, created INTEGER NOT NULL, expires INTEGER NOT NULL, status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY, at INTEGER NOT NULL, received INTEGER NOT NULL,
                status TEXT NOT NULL, source TEXT NOT NULL, command_id TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS events_at ON events(at);
            CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, value TEXT NOT NULL);
        """)
        # Upgrade in place: existing commands and attendance history remain intact.
        for table, column, definition in (("commands", "kind", "TEXT NOT NULL DEFAULT 'punch'"),
                                          ("commands", "payload", "TEXT NOT NULL DEFAULT '{}'"),
                                          ("events", "kind", "TEXT NOT NULL DEFAULT 'punch'")):
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        conn.execute("INSERT OR IGNORE INTO settings VALUES(1,1,?)", (json.dumps(DEFAULT_SETTINGS),))

    def settings(conn):
        row = conn.execute("SELECT revision,value FROM settings WHERE id=1").fetchone()
        return row["revision"], json.loads(row["value"])

    def same_origin():
        origin = request.headers.get("Origin")
        if (origin and origin.rstrip("/") != request.host_url.rstrip("/")) or request.headers.get("Sec-Fetch-Site") == "cross-site":
            abort(403, "拒绝跨站请求")

    def expire(conn, now):
        conn.execute("UPDATE commands SET status='expired' WHERE status='pending' AND expires<=?", (now,))
        conn.execute("UPDATE commands SET status='unknown' WHERE status='sent' AND expires<=?", (now,))

    def device_json():
        supplied = request.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied.encode(), ("Bearer " + app.config["DEVICE_TOKEN"]).encode()):
            abort(401)
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or data.get("device") != app.config["DEVICE_ID"]:
            abort(400, "invalid device")
        return data

    def identifier(value):
        return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @app.errorhandler(400)
    @app.errorhandler(401)
    @app.errorhandler(403)
    @app.errorhandler(409)
    @app.errorhandler(413)
    def error(exc):
        if request.path.startswith("/api/"):
            return jsonify(error=exc.description), exc.code
        return exc.description, exc.code, {"Content-Type": "text/plain; charset=utf-8"}

    @app.get("/healthz")
    def health():
        with db() as conn:
            conn.execute("SELECT 1 FROM device LIMIT 1").fetchone()
        return jsonify(ok=True)

    @app.post("/api/poll")
    def poll():
        data = device_json()
        temperature = data.get("temperature")
        if not (type(data.get("time_valid")) is bool and type(data.get("storage_ok")) is bool
                and integer(data.get("pending"), 0, 16) and integer(data.get("rssi"), -150, 0)
                and (temperature is None or (type(temperature) in (int, float) and -40 <= temperature <= 150))
                and integer(data.get("start"), 0, 86399) and integer(data.get("end"), data["start"], 86399)
                and integer(data.get("last_date"), 0, 99991231)):
            abort(400, "invalid heartbeat")
        protocol = data.get("protocol", 1)
        if not integer(protocol, 1, 2):
            abort(400, "invalid protocol")
        if protocol == 2 and not (integer(data.get("revision"), 0, 2147483647)
                and integer(data.get("uptime"), 0, 4294967295)
                and integer(data.get("heap"), 0, 10000000)
                and integer(data.get("reset_reason"), 0, 32)
                and integer(data.get("last_http"), -1000, 599)):
            abort(400, "invalid diagnostics")
        now = int(time.time())
        with db() as conn:
            conn.execute("""INSERT INTO device VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                         last_seen=excluded.last_seen, info=excluded.info""",
                         (data["device"], now, now, json.dumps(data)))
            expire(conn, now)
            revision, current_settings = settings(conn)
            command = None
            if data["time_valid"] and data["storage_ok"] and data["pending"] == 0:
                command = conn.execute("SELECT id,expires,kind,payload FROM commands WHERE status IN ('pending','sent') AND (?=2 OR kind='punch') ORDER BY created LIMIT 1", (protocol,)).fetchone()
                if command:
                    conn.execute("UPDATE commands SET status='sent' WHERE id=?", (command["id"],))
                    command = dict(command)
                    command["payload"] = json.loads(command["payload"])
                    if command["kind"] == "punch" and not command["payload"]:
                        command["payload"] = {"settings": current_settings}  # Pre-upgrade queued command.
        result = dict(command=command)
        if protocol == 2:
            result.update(server_time=now, revision=revision, settings=current_settings)
        return jsonify(result)

    @app.post("/api/events")
    def event():
        data = device_json()
        event = data.get("event")
        now = int(time.time())
        if not isinstance(event, dict):
            abort(400, "invalid event")
        kind = event.get("kind", "punch")
        if not (identifier(event.get("id")) and integer(event.get("at"), 1704067200, now + 60)
                and event.get("status") in ("completed", "failed", "interrupted")
                and isinstance(kind, str) and kind in KINDS and event.get("source") in ("automatic", "manual")
                and (event["source"] != "automatic" or kind == "punch")
                and ((event["source"] == "automatic" and event.get("command") == "")
                     or (event["source"] == "manual" and identifier(event.get("command"))))):
            abort(400, "invalid event fields")
        values = (event["at"], event["status"], event["source"], event["command"], kind)
        with db() as conn:
            existing = conn.execute("SELECT at,status,source,command_id,kind FROM events WHERE id=?", (event["id"],)).fetchone()
            if existing and tuple(existing) != values:
                abort(409, "event ID already contains a different result")
            conn.execute("INSERT OR IGNORE INTO events(id,at,received,status,source,command_id,kind) VALUES(?,?,?,?,?,?,?)",
                         (event["id"], event["at"], now, event["status"], event["source"], event["command"], kind))
            if event["command"]:
                # Late acknowledgements must also resolve a previously timed-out command.
                conn.execute("UPDATE commands SET status=? WHERE id=?", (event["status"], event["command"]))
        return jsonify(ok=True, id=event["id"])

    @app.post("/manual")
    def manual():
        same_origin()
        command_id = request.form.get("request_id")
        kind = request.form.get("kind", "punch")
        if kind not in KINDS:
            abort(400, "无效指令")
        if not identifier(command_id):
            abort(400, "请刷新页面后重试")
        now = int(time.time())
        with db() as conn:
            expire(conn, now)
            if conn.execute("SELECT 1 FROM commands WHERE id=?", (command_id,)).fetchone():
                return redirect(url_for("dashboard"), code=303)
            device = conn.execute("SELECT * FROM device WHERE id=?", (app.config["DEVICE_ID"],)).fetchone()
            info = json.loads(device["info"]) if device else {}
            if not device or now - device["last_seen"] > 30 or not info.get("time_valid") or not info.get("storage_ok") or info.get("pending"):
                abort(409, "设备离线、未校时或仍有待上传记录，请等设备就绪后重试")
            if kind != "punch" and info.get("protocol", 1) != 2:
                abort(409, "请先烧录新版 ESP32 固件")
            revision, current_settings = settings(conn)
            if info.get("protocol") == 2 and info.get("revision") != revision:
                abort(409, "设备尚未应用最新配置，请稍后刷新")
            payload = {"settings": current_settings} if kind in ("punch", "test") else {}
            if kind == "position":
                try:
                    pulse, hold = int(request.form["pulse"]), int(request.form["hold"])
                except (KeyError, ValueError):
                    abort(400, "请输入舵机脉宽和保持时间")
                if not (integer(pulse, 400, 2500) and integer(hold, 100, 5000)):
                    abort(400, "脉宽范围 400–2500 μs，保持时间范围 100–5000 ms")
                payload = dict(pulse=pulse, hold=hold)
            if conn.execute("SELECT 1 FROM commands WHERE status IN ('pending','sent') OR created+CASE WHEN kind='position' THEN 5 ELSE 30 END>?", (now,)).fetchone():
                abort(409, "已有待执行指令或刚刚执行过，请稍后再试")
            conn.execute("INSERT INTO commands(id,created,expires,status,kind,payload) VALUES(?,?,?,'pending',?,?)", (command_id, now, now + 120, kind, json.dumps(payload)))
        return redirect(url_for("dashboard"), code=303)

    @app.post("/settings")
    def update_settings():
        same_origin()
        try:
            value = form_settings(request.form)
            submitted_revision = int(request.form["revision"])
        except (KeyError, ValueError):
            abort(400, "配置无效：结束时间不能早于开始时间；重启时间不能在打卡窗口内；脉宽 400–2500 μs，单步 20–5000 ms，1–16 步，重复 1–5 次，总动作不得超过 20 秒")
        with db() as conn:
            revision, current = settings(conn)
            if submitted_revision != revision:
                abort(409, "配置已在其他页面修改，请刷新后重试")
            if value != current:
                conn.execute("UPDATE settings SET revision=revision+1,value=? WHERE id=1", (json.dumps(value),))
        return redirect(url_for("dashboard"), code=303)

    def local_date(epoch):
        return datetime.fromtimestamp(epoch, LOCAL_TZ).date()

    app.jinja_env.filters["localtime"] = lambda value: datetime.fromtimestamp(value, LOCAL_TZ).strftime("%m-%d %H:%M:%S")
    app.jinja_env.filters["clock"] = lambda value: f"{value // 3600:02d}:{value // 60 % 60:02d}"

    @app.get("/")
    def dashboard():
        now = int(time.time())
        today = local_date(now)
        cutoff = int(datetime.combine(today - timedelta(days=13), datetime.min.time(), LOCAL_TZ).timestamp())
        with db() as conn:
            expire(conn, now)
            device = conn.execute("SELECT * FROM device WHERE id=?", (app.config["DEVICE_ID"],)).fetchone()
            events = conn.execute("SELECT * FROM events WHERE at>=? ORDER BY at DESC,received DESC", (cutoff,)).fetchall()
            commands = conn.execute("SELECT * FROM commands ORDER BY created DESC LIMIT 10").fetchall()
            revision, current_settings = settings(conn)
        info = json.loads(device["info"]) if device else {}
        online = bool(device and now - device["last_seen"] <= 30)
        ready = online and info.get("time_valid") and info.get("storage_ok") and info.get("pending") == 0
        ready = ready and (info.get("protocol", 1) == 1 or info.get("revision") == revision)
        busy = any(c["status"] in ("pending", "sent") or c["created"] + (5 if c["kind"] == "position" else 30) > now for c in commands)
        days = []
        for offset in range(14):
            day = today - timedelta(days=offset)
            records = [e for e in events if e["kind"] == "punch" and local_date(e["at"]) == day]
            if records:
                status = LABELS[records[0]["status"]]
            elif not device or day < local_date(device["first_seen"]):
                status = "未接入监测"
            elif day == today and not current_settings["enabled"]:
                status = "自动打卡已暂停"
            elif day == today:
                t = datetime.fromtimestamp(now, LOCAL_TZ)
                seconds = t.hour * 3600 + t.minute * 60 + t.second
                start, end = (current_settings["start"], current_settings["end"]) if info.get("protocol") == 2 else (info["start"], info["end"])
                status = "未到时间" if seconds < start else "等待执行／上报" if seconds <= end else "缺少执行记录"
            else:
                status = "缺少执行记录"
            days.append(dict(date=day.isoformat(), status=status, count=len(records)))
        return render_template("index.html", device=device, info=info, online=online,
                               ready=ready and not busy, days=days, events=events[:30], commands=commands,
                               labels=LABELS, kinds=KINDS, settings=current_settings, revision=revision,
                               request_ids={kind: secrets.token_hex(16) for kind in KINDS})

    return app


if __name__ == "__main__":
    from waitress import serve
    serve(create_app(), host="0.0.0.0", port=int(os.getenv("PORT", "8888")), threads=4)
