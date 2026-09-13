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
        """)

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

    def integer(value, low, high):
        return type(value) is int and low <= value <= high

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
        now = int(time.time())
        with db() as conn:
            conn.execute("""INSERT INTO device VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                         last_seen=excluded.last_seen, info=excluded.info""",
                         (data["device"], now, now, json.dumps(data)))
            expire(conn, now)
            command = None
            if data["time_valid"] and data["storage_ok"] and data["pending"] == 0:
                command = conn.execute("SELECT id,expires FROM commands WHERE status IN ('pending','sent') ORDER BY created LIMIT 1").fetchone()
                if command:
                    conn.execute("UPDATE commands SET status='sent' WHERE id=?", (command["id"],))
        return jsonify(command=dict(command) if command else None)

    @app.post("/api/events")
    def event():
        data = device_json()
        event = data.get("event")
        now = int(time.time())
        if not isinstance(event, dict):
            abort(400, "invalid event")
        if not (identifier(event.get("id")) and integer(event.get("at"), 1704067200, now + 60)
                and event.get("status") in ("completed", "failed", "interrupted")
                and event.get("source") in ("automatic", "manual")
                and ((event["source"] == "automatic" and event.get("command") == "")
                     or (event["source"] == "manual" and identifier(event.get("command"))))):
            abort(400, "invalid event fields")
        values = (event["at"], event["status"], event["source"], event["command"])
        with db() as conn:
            existing = conn.execute("SELECT at,status,source,command_id FROM events WHERE id=?", (event["id"],)).fetchone()
            if existing and tuple(existing) != values:
                abort(409, "event ID already contains a different result")
            conn.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?)",
                         (event["id"], event["at"], now, event["status"], event["source"], event["command"]))
            if event["command"]:
                # Late acknowledgements must also resolve a previously timed-out command.
                conn.execute("UPDATE commands SET status=? WHERE id=?", (event["status"], event["command"]))
        return jsonify(ok=True, id=event["id"])

    @app.post("/manual")
    def manual():
        origin = request.headers.get("Origin")
        if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
            abort(403, "拒绝跨站请求")
        command_id = request.form.get("request_id")
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
            if conn.execute("SELECT 1 FROM commands WHERE status IN ('pending','sent') OR created>?", (now - 30,)).fetchone():
                abort(409, "已有待执行指令或刚刚执行过，请稍后再试")
            conn.execute("INSERT INTO commands VALUES(?,?,?,'pending')", (command_id, now, now + 120))
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
        info = json.loads(device["info"]) if device else {}
        online = bool(device and now - device["last_seen"] <= 30)
        ready = online and info.get("time_valid") and info.get("storage_ok") and info.get("pending") == 0
        busy = any(c["status"] in ("pending", "sent") or c["created"] > now - 30 for c in commands)
        days = []
        for offset in range(14):
            day = today - timedelta(days=offset)
            records = [e for e in events if local_date(e["at"]) == day]
            if records:
                status = LABELS[records[0]["status"]]
            elif not device or day < local_date(device["first_seen"]):
                status = "未接入监测"
            elif day == today:
                t = datetime.fromtimestamp(now, LOCAL_TZ)
                seconds = t.hour * 3600 + t.minute * 60 + t.second
                status = "未到时间" if seconds < info["start"] else "等待执行／上报" if seconds <= info["end"] else "缺少执行记录"
            else:
                status = "缺少执行记录"
            days.append(dict(date=day.isoformat(), status=status, count=len(records)))
        return render_template("index.html", device=device, info=info, online=online,
                               ready=ready and not busy, days=days, events=events[:30], commands=commands,
                               labels=LABELS, request_id=secrets.token_hex(16))

    return app


if __name__ == "__main__":
    from waitress import serve
    serve(create_app(), host="0.0.0.0", port=int(os.getenv("PORT", "8888")), threads=4)
