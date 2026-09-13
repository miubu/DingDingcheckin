"""Run: python -m unittest -v test_app.py (no third-party test runner)."""
import secrets
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from app import create_app


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
                conn.execute("INSERT INTO commands VALUES(?,?,?,'pending')", (secrets.token_hex(16), now-200, now-1))
                conn.execute("INSERT INTO commands VALUES(?,?,?,'sent')", (sent, now-200, now-1))
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


if __name__ == "__main__":
    unittest.main()
