"""Run: python -m unittest -v test_app.py (no third-party test runner)."""
import secrets
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from app import create_app
from control import DEFAULT_SETTINGS, valid_settings


class FlowTest(unittest.TestCase):
    def test_command_retry_expiry_and_durable_results(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "test.db")
            config = {"TESTING": True, "DATABASE": path, "DEVICE_TOKEN": "device-test-" * 4}
            app = create_app(config)
            client = app.test_client()
            headers = {"Authorization": "Bearer " + config["DEVICE_TOKEN"]}
            heartbeat = dict(device="desk-esp32", time_valid=True, storage_ok=True,
                             pending=0, rssi=-40, temperature=43.2,
                             start=29400, end=31259, last_date=0)
            self.assertEqual(client.get("/").status_code, 200)
            self.assertEqual(client.post("/api/poll", json=heartbeat).status_code, 401)
            for bad in ([], None, {"device": "wrong"}, {**heartbeat, "pending": True},
                        {**heartbeat, "end": "08:40"}):
                self.assertEqual(client.post("/api/poll", headers=headers, json=bad).status_code, 400)
            request_id = secrets.token_hex(16)
            form = {"request_id": request_id}
            self.assertEqual(client.post("/manual", headers={"Origin": "https://example.com"}, data=form).status_code, 403)
            self.assertEqual(client.post("/manual", data=form).status_code, 409)  # Offline.
            poll = lambda: client.post("/api/poll", headers=headers, json=heartbeat)
            self.assertIsNone(poll().json["command"])
            self.assertEqual(client.post("/manual", data=form).status_code, 303)
            self.assertEqual(client.post("/manual", data=form).status_code, 303)  # Double submit.
            first = poll().json["command"]
            self.assertEqual(first["id"], request_id)
            self.assertEqual(poll().json["command"], first)  # Lost response -> same command.
            heartbeat["pending"] = 1
            self.assertIsNone(poll().json["command"])  # Device drains outbox first.
            heartbeat["pending"] = 0
            second_form = {"request_id": secrets.token_hex(16)}
            self.assertEqual(client.post("/manual", data=second_form).status_code, 409)
            event = dict(id=secrets.token_hex(16), at=int(time.time()), source="manual",
                         status="completed", command=request_id)
            report = lambda e: client.post("/api/events", headers=headers, json={"device": "desk-esp32", "event": e})
            self.assertEqual(report(event).status_code, 200)
            self.assertEqual(report(event).status_code, 200)  # Lost acknowledgement -> one row.
            self.assertEqual(report({**event, "status": "failed"}).status_code, 409)
            self.assertEqual(report({**event, "at": "bad"}).status_code, 400)
            self.assertIsNone(poll().json["command"])
            with sqlite3.connect(path) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT status FROM commands").fetchone()[0], "completed")
                # Insert both never-delivered and delivered commands that have expired.
                now = int(time.time())
                sent = secrets.token_hex(16)
                conn.execute("INSERT INTO commands(id,created,expires,status) VALUES(?,?,?,'pending')", (secrets.token_hex(16), now-200, now-1))
                conn.execute("INSERT INTO commands(id,created,expires,status) VALUES(?,?,?,'sent')", (sent, now-200, now-1))
            self.assertIsNone(poll().json["command"])
            late = dict(id=secrets.token_hex(16), at=int(time.time()), source="manual", status="interrupted", command=sent)
            self.assertEqual(report(late).status_code, 200)
            recreated = create_app(config).test_client()
            self.assertEqual(recreated.get("/healthz").status_code, 200)
            with sqlite3.connect(path) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 2)
                self.assertEqual(conn.execute("SELECT status FROM commands WHERE id=?", (sent,)).fetchone()[0], "interrupted")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM commands WHERE status='expired'").fetchone()[0], 1)
            page = client.get("/").get_data(as_text=True)
            self.assertIn("动作中断", page)
            self.assertIn("每天 08:10", page)
            self.assertIn("芯片 43.2 °C", page)
            self.assertIn("未接入监测", page)
            self.assertNotIn(config["DEVICE_TOKEN"], page)

    def test_web_configuration_and_v2_controls(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "test.db")
            config = {"TESTING": True, "DATABASE": path, "DEVICE_TOKEN": "device-test-" * 4}
            client = create_app(config).test_client()
            headers = {"Authorization": "Bearer " + config["DEVICE_TOKEN"]}
            heartbeat = dict(device="desk-esp32", protocol=2, time_valid=False, storage_ok=True,
                             pending=0, rssi=-40, start=29400, end=31259, last_date=0,
                             revision=0, uptime=100, heap=100000, reset_reason=9, last_http=200)
            poll = lambda: client.post("/api/poll", headers=headers, json=heartbeat)
            reply = poll().json
            self.assertEqual(reply["settings"], DEFAULT_SETTINGS)
            self.assertLess(abs(reply["server_time"] - time.time()), 2)
            self.assertIsNone(reply["command"])
            form = dict(revision=1, enabled="on", start="09:00", end="09:30", reboot_enabled="on",
                        reboot_at="04:00", home="1500", repeats="2", steps="1500,200\n700,200")
            self.assertEqual(client.post("/settings", data=form, headers={"Origin": "https://bad.example"}).status_code, 403)
            for bad in ({**form, "home": "399"}, {**form, "end": "08:00"},
                        {**form, "reboot_at": "09:01"}, {**form, "steps": "1500,5000\n700,5000"},
                        {**form, "steps": "1500,200\n" * 17}, {**form, "start": "25:00"}):
                self.assertEqual(client.post("/settings", data=bad).status_code, 400)
            self.assertEqual(client.post("/settings", data=form).status_code, 303)
            self.assertEqual(client.post("/settings", data=form).status_code, 409)  # Stale edit.
            heartbeat["time_valid"] = True
            heartbeat["revision"] = 1
            command_form = dict(request_id=secrets.token_hex(16), kind="position", pulse="700", hold="1000")
            self.assertEqual(client.post("/manual", data=command_form).status_code, 409)  # Unapplied config.
            heartbeat["revision"] = 2
            poll()
            self.assertEqual(client.post("/manual", data={**command_form, "pulse": "2501"}).status_code, 400)
            self.assertEqual(client.post("/manual", data=command_form).status_code, 303)
            command = poll().json["command"]
            self.assertEqual(command["kind"], "position")
            self.assertEqual(command["payload"], dict(pulse=700, hold=1000))
            event = dict(id=secrets.token_hex(16), at=int(time.time()), source="manual", kind="position",
                         status="completed", command=command["id"])
            def report(e):
                return client.post("/api/events", headers=headers, json={"device": "desk-esp32", "event": e})
            self.assertEqual(report(event).status_code, 200)
            self.assertEqual(report(event).status_code, 200)
            self.assertEqual(report({**event, "kind": "punch"}).status_code, 409)
            self.assertEqual(report({**event, "kind": []}).status_code, 400)
            page = client.get("/").get_data(as_text=True)
            self.assertIn("0 条打卡记录", page)
            self.assertIn("掉压／供电异常", page)
            self.assertIn("设备已应用", page)
            self.assertNotIn('http-equiv="refresh"', page)
            with sqlite3.connect(path) as conn:
                conn.execute("UPDATE commands SET created=0")  # End the safety cooldown without sleeping.
            for kind in ("test", "punch", "reboot"):
                cid = secrets.token_hex(16)
                self.assertEqual(client.post("/manual", data=dict(request_id=cid, kind=kind)).status_code, 303)
                issued = poll().json["command"]
                self.assertEqual(issued["kind"], kind)
                if kind != "reboot":
                    self.assertEqual(issued["payload"]["settings"]["start"], 32400)
                self.assertEqual(report({**event, "id": secrets.token_hex(16), "command": cid, "kind": kind}).status_code, 200)
                with sqlite3.connect(path) as conn:
                    conn.execute("UPDATE commands SET created=0")
            self.assertIn("1 条打卡记录", client.get("/").get_data(as_text=True))
            recreated = create_app(config).test_client()
            self.assertEqual(recreated.post("/api/poll", headers=headers, json=heartbeat).json["revision"], 2)

    def test_migration_preserves_old_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "old.db")
            with sqlite3.connect(path) as conn:
                conn.executescript("""CREATE TABLE commands(id TEXT PRIMARY KEY,created INTEGER,expires INTEGER,status TEXT);
                    CREATE TABLE events(id TEXT PRIMARY KEY,at INTEGER,received INTEGER,status TEXT,source TEXT,command_id TEXT);
                    INSERT INTO commands VALUES('old',1,2,'completed');
                    INSERT INTO events VALUES('old',1704067200,1704067200,'completed','automatic','');""")
            config = {"DATABASE": path, "DEVICE_TOKEN": "x" * 32}
            create_app(config)
            create_app(config)  # Migration is idempotent.
            with sqlite3.connect(path) as conn:
                self.assertEqual(conn.execute("SELECT kind,status FROM events").fetchone(), ("punch", "completed"))
                self.assertEqual(conn.execute("SELECT kind,status FROM commands").fetchone(), ("punch", "completed"))
                cid = secrets.token_hex(16)
                conn.execute("INSERT INTO commands(id,created,expires,status) VALUES(?,?,?,'pending')", (cid, int(time.time()), int(time.time()) + 120))
            heartbeat = dict(device="desk-esp32", time_valid=True, storage_ok=True,
                             pending=0, rssi=-40, start=29400, end=31259, last_date=0)
            response = create_app(config).test_client().post("/api/poll", json=heartbeat,
                headers={"Authorization": "Bearer " + config["DEVICE_TOKEN"]})
            self.assertEqual(response.json["command"]["payload"]["settings"], DEFAULT_SETTINGS)

    def test_settings_limits(self):
        self.assertTrue(valid_settings(DEFAULT_SETTINGS))
        for invalid in (None, [], {}, {**DEFAULT_SETTINGS, "repeats": True},
                        {**DEFAULT_SETTINGS, "steps": [[700, 10]]},
                        {**DEFAULT_SETTINGS, "steps": [[700, 5000]] * 16}):
            self.assertFalse(valid_settings(invalid))


if __name__ == "__main__":
    unittest.main()
