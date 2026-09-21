import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from app import create_server  # noqa: E402
from airworthiness.service import AirworthinessService  # noqa: E402
from airworthiness.store import Store  # noqa: E402

T_REG = "2026-09-21T08:00:00+08:00"
T_INSTALL = "2026-09-21T09:00:00+08:00"
T_SIGN = "2026-09-21T09:30:00+08:00"
T_START = "2026-09-21T11:00:00+08:00"
T_END = "2026-09-21T11:30:00+08:00"
QUAL_UNTIL = "2026-12-31T23:59:59+08:00"
SIG_UNTIL = "2026-09-23T09:30:00+08:00"


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = create_server(service=AirworthinessService(Store()), port=0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        # 每个用例使用全新的空存储，互不污染
        self.server.service = AirworthinessService(Store())

    def api(self, method, path, role=None, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if role is not None:
            request.add_header("X-Role", role)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def _seed_airframe(self, battery_synced=True, signer_qualified=True):
        self.api("POST", "/airframes", "maintenance", {
            "id": "AF-1", "model": "quad-x", "serial": "SN-1",
            "positions": {"battery-1": "battery", "propeller-1": "propeller"},
            "approved_firmware": ["fw-1.1"],
            "required_inspections": ["preflight", "propeller"],
            "compatible_payloads": ["camera"],
            "max_payload_kg": 2.0, "max_wind_mps": 12.0,
            "min_temp_c": -10.0, "max_temp_c": 40.0,
        })
        battery = {"serial": "BAT-1", "type": "battery", "model": "m1",
                   "cycle_limit": 100, "hour_limit": 50.0}
        if battery_synced:
            battery["life_synced_at"] = T_REG
        self.api("POST", "/parts", "maintenance", battery)
        self.api("POST", "/parts", "maintenance", {
            "serial": "PROP-1", "type": "propeller", "model": "m1",
            "cycle_limit": 100, "hour_limit": 50.0, "life_synced_at": T_REG,
        })
        self.api("POST", "/airframes/AF-1/install", "maintenance",
                 {"part_serial": "BAT-1", "position": "battery-1", "at": T_INSTALL})
        self.api("POST", "/airframes/AF-1/install", "maintenance",
                 {"part_serial": "PROP-1", "position": "propeller-1", "at": T_INSTALL})
        self.api("POST", "/airframes/AF-1/firmware", "maintenance",
                 {"version": "fw-1.1", "at": T_INSTALL})
        self.api("POST", "/qualifications", "maintenance", {
            "personnel_id": "张工", "kind": "preflight",
            "valid_from": T_REG, "valid_until": QUAL_UNTIL,
        })
        propeller_qual_until = QUAL_UNTIL if signer_qualified else "2026-01-01T00:00:00+08:00"
        self.api("POST", "/qualifications", "maintenance", {
            "personnel_id": "张工", "kind": "propeller",
            "valid_from": "2025-01-01T00:00:00+08:00", "valid_until": propeller_qual_until,
        })
        for kind in ("preflight", "propeller"):
            self.api("POST", "/airframes/AF-1/inspections", "maintenance", {
                "inspection_kind": kind, "personnel_id": "张工",
                "signed_at": T_SIGN, "result": "pass", "valid_until": SIG_UNTIL,
            })
        self.api("POST", "/missions", "dispatcher", {
            "id": "M-1", "airframe_id": "AF-1",
            "payload": {"kind": "camera", "weight_kg": 1.0},
            "planned_start": T_START, "planned_end": T_END,
            "environment": {"wind_mps": 5.0, "temp_c": 20.0, "precipitation": False},
        })

    def test_role_header_required(self):
        status, payload = self.api("GET", "/missions/none/release")
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "forbidden")

    def test_dispatcher_view_hides_maintenance_detail(self):
        self._seed_airframe(battery_synced=False, signer_qualified=False)

        status, decision = self.api("GET", "/missions/M-1/release", "dispatcher")
        self.assertEqual(status, 200)
        self.assertEqual(decision["status"], "blocked")
        codes = {b["code"] for b in decision["blockers"]}
        self.assertIn("PART_LIFE_UNSYNCED", codes)
        self.assertIn("INSPECTION_SIGNATURE_INVALID", codes)
        # 调度视图只有结论与阻断项：无寿命明细、无签名依据、无人员身份
        self.assertNotIn("life_basis", decision)
        self.assertNotIn("signature_basis", decision)
        self.assertNotIn("detail", decision["blockers"][0])
        self.assertNotIn("张工", json.dumps(decision, ensure_ascii=False))

        status, decision = self.api("GET", "/missions/M-1/release", "maintenance")
        self.assertEqual(status, 200)
        self.assertIn("life_basis", decision)
        self.assertIn("signature_basis", decision)
        battery_basis = next(b for b in decision["life_basis"] if b["serial"] == "BAT-1")
        self.assertFalse(battery_basis["life_synced"])
        propeller_basis = next(b for b in decision["signature_basis"] if b["kind"] == "propeller")
        self.assertEqual(propeller_basis["personnel_id"], "张工")
        self.assertFalse(propeller_basis["qualification_valid"])

    def test_dispatcher_cannot_read_config_or_history(self):
        self._seed_airframe()
        status, _ = self.api("GET", "/airframes/AF-1/config", "dispatcher")
        self.assertEqual(status, 403)
        status, _ = self.api("GET", "/airframes/AF-1/history", "dispatcher")
        self.assertEqual(status, 403)
        status, config = self.api("GET", "/airframes/AF-1/config?at=2026-09-21T10:00:00%2B08:00", "maintenance")
        self.assertEqual(status, 200)
        self.assertEqual(config["firmware_version"], "fw-1.1")
        self.assertEqual({p["serial"] for p in config["parts"]}, {"BAT-1", "PROP-1"})
        status, history = self.api("GET", "/airframes/AF-1/history", "maintenance")
        self.assertEqual(status, 200)
        self.assertEqual(len(history["events"]), 2)

    def test_dispatch_blocked_then_released_via_api(self):
        self._seed_airframe(battery_synced=False)

        status, payload = self.api("POST", "/missions/M-1/dispatch", "dispatcher", {})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "release_blocked")
        self.assertEqual(payload["decision"]["status"], "blocked")
        self.assertIn("PART_LIFE_UNSYNCED", {b["code"] for b in payload["decision"]["blockers"]})

        status, _ = self.api("POST", "/parts/BAT-1/life-sync", "maintenance",
                             {"used_cycles": 12, "used_hours": 6.0, "at": T_SIGN})
        self.assertEqual(status, 200)

        status, decision = self.api("POST", "/missions/M-1/dispatch", "dispatcher", {})
        self.assertEqual(status, 200)
        self.assertEqual(decision["status"], "released")
        self.assertIsNotNone(decision["config_id"])

        status, report = self.api("POST", "/flight-logs", "maintenance", {
            "id": "L-1", "mission_id": "M-1",
            "started_at": T_START, "ended_at": T_END, "cycles": 1, "hours": 0.5,
        })
        self.assertEqual(status, 201)
        self.assertFalse(report["late"])
        self.assertEqual(report["affected_missions"], [])

    def test_maintenance_cannot_create_mission_and_dispatcher_cannot_mutate_fleet(self):
        self._seed_airframe()
        status, _ = self.api("POST", "/missions", "maintenance", {
            "id": "M-2", "airframe_id": "AF-1",
            "payload": {"kind": "camera", "weight_kg": 1.0},
            "planned_start": T_START, "planned_end": T_END,
            "environment": {"wind_mps": 5.0, "temp_c": 20.0},
        })
        self.assertEqual(status, 403)
        status, _ = self.api("POST", "/parts", "dispatcher", {
            "serial": "BAT-9", "type": "battery", "model": "m1",
        })
        self.assertEqual(status, 403)
        status, _ = self.api("POST", "/emergency-releases", "dispatcher", {
            "id": "ER-1", "airframe_id": "AF-1", "requested_by": "调度甲",
            "approved_by": "总工", "approved_at": T_SIGN, "expires_at": QUAL_UNTIL,
            "overrides": ["PART_LIFE_UNSYNCED"], "conditions": "测试",
        })
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
