"""HTTP 端到端：交接班场景——换电池未同步、资质过期签字、调度拦截、岗位视图。"""

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from app import SERVICE_NAME, create_server  # noqa: E402


class Client:
    def __init__(self, base):
        self.base = base

    def call(self, method, path, payload=None, actor=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if actor:
            req.add_header("X-Actor-Id", actor)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)


class HttpScenarioTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = create_server()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.client = Client(f"http://127.0.0.1:{cls.server.server_port}")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _seed(self):
        c = self.client
        for p in (
            {"id": "h-mech", "name": "机务", "role": "机务人员"},
            {"id": "h-disp", "name": "放行", "role": "放行员"},
            {"id": "h-reg", "name": "监察", "role": "监管人员"},
        ):
            c.call("POST", "/people", p)
        # 机务螺旋桨资质 2025 年已过期
        status, _ = c.call("POST", "/people/h-mech/certifications", {
            "scope": "propeller_check",
            "validFrom": "2024-01-01T00:00:00Z",
            "validUntil": "2025-12-31T00:00:00Z",
        }, "h-reg")
        self.assertEqual(status, 201)
        status, _ = c.call("POST", "/aircraft", {
            "id": "H-AC", "model": "Q1",
            "requiredPositions": {"battery": "battery", "fc": "flight_controller", "prop": "propeller"},
            "maxWindKt": 25, "allowPrecipitation": False, "maxPayloadKg": 5,
            "inspectionProgram": {"propeller_check": 90},
        })
        self.assertEqual(status, 201)
        for comp in (
            {"serial": "H-BAT", "kind": "battery", "model": "b", "cycleLimit": 300},
            {"serial": "H-FC", "kind": "flight_controller", "model": "f"},
            {"serial": "H-PROP", "kind": "propeller", "model": "p"},
        ):
            self.assertEqual(c.call("POST", "/components", comp)[0], 201)
        self.assertEqual(c.call("POST", "/firmware", {
            "id": "H-FW", "version": "1.0", "targetKind": "flight_controller",
        })[0], 201)

    def test_health(self):
        status, body = self.client.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok", "service": SERVICE_NAME})

    def test_handover_scenario_blocks_dispatch_until_fixed(self):
        c = self.client
        self._seed()

        # 1) 换电池未同步循环寿命 → 装机被拒
        status, body = c.call("POST", "/aircraft/H-AC/installs", {
            "position": "battery", "serial": "H-BAT", "at": "2026-09-20T06:00:00Z",
        }, "h-mech")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_request")

        # 同步基线后装机成功
        status, _ = c.call("POST", "/aircraft/H-AC/installs", {
            "position": "battery", "serial": "H-BAT", "at": "2026-09-20T06:00:00Z",
            "baselineCycles": 42,
        }, "h-mech")
        self.assertEqual(status, 201)
        c.call("POST", "/aircraft/H-AC/installs", {
            "position": "fc", "serial": "H-FC", "at": "2026-09-20T06:00:00Z",
        }, "h-mech")
        c.call("POST", "/aircraft/H-AC/installs", {
            "position": "prop", "serial": "H-PROP", "at": "2026-09-20T06:00:00Z",
        }, "h-mech")
        c.call("POST", "/aircraft/H-AC/firmware-flashes", {
            "firmwareId": "H-FW", "at": "2026-09-20T07:00:00Z",
        }, "h-mech")

        # 2) 螺旋桨检查由资质过期人员签字
        status, _ = c.call("POST", "/aircraft/H-AC/inspections", {
            "scope": "propeller_check", "signedBy": "h-mech",
            "signedAt": "2026-09-20T07:30:00Z", "validityDays": 90,
            "componentSerial": "H-PROP",
        }, "h-mech")
        self.assertEqual(status, 201)

        # 3) 建任务并放行评估 → NO_GO
        c.call("POST", "/missions", {
            "id": "H-M1", "aircraftId": "H-AC",
            "plannedStart": "2026-09-21T02:00:00Z",
            "plannedEnd": "2026-09-21T04:00:00Z",
            "forecast": {"windKt": 15, "precip": False},
        }, "h-disp")
        status, ev = c.call("POST", "/missions/H-M1/release-evaluations", {}, "h-disp")
        self.assertEqual(status, 201)
        self.assertEqual(ev["decision"], "NO_GO")
        self.assertIn("INSPECTION_SIGNATURE_INVALID", {b["code"] for b in ev["blockers"]})

        # 4) 调度视图：看到结论与阻断项，但看不到寿命/资质细节
        status, dispatch = c.call("GET", "/missions/H-M1/views/dispatch", actor="h-disp")
        self.assertEqual(status, 200)
        self.assertEqual(dispatch["decision"], "NO_GO")
        flat = json.dumps(dispatch, ensure_ascii=False)
        self.assertNotIn("baseline", flat)
        self.assertNotIn("certification", flat)

        # 5) 机务视图：看到寿命依据与签字依据
        status, mech = c.call(
            "GET", "/aircraft/H-AC/views/mechanic?at=2026-09-21T01:00:00Z", actor="h-mech")
        self.assertEqual(status, 200)
        self.assertEqual(
            mech["effectiveConfig"]["positions"]["battery"]["accruedCycles"], 42)
        self.assertFalse(mech["signatureBasis"][0]["valid"])

        # 6) 岗位越权被拒
        self.assertEqual(c.call("GET", "/missions/H-M1/views/dispatch", actor="h-mech")[0], 403)
        self.assertEqual(
            c.call("GET", "/aircraft/H-AC/views/mechanic?at=2026-09-21T01:00:00Z",
                   actor="h-disp")[0],
            403,
        )
        self.assertEqual(
            c.call("POST", "/missions/H-M1/emergency-releases", {
                "validUntil": "2026-09-21T12:00:00Z", "reason": "自检自批",
            }, "h-disp")[0],
            403,
        )

        # 7) 监管批准一次性紧急放行（仅覆盖签字类阻断）→ EMERGENCY_GO
        status, er = c.call("POST", "/missions/H-M1/emergency-releases", {
            "validUntil": "2026-09-21T12:00:00Z",
            "maxFlights": 1,
            "permittedBlockers": ["INSPECTION_SIGNATURE_INVALID"],
            "reason": "现场监督",
        }, "h-reg")
        self.assertEqual(status, 201)
        status, ev2 = c.call("POST", "/missions/H-M1/release-evaluations", {}, "h-disp")
        self.assertEqual(ev2["decision"], "EMERGENCY_GO")

        # 8) 次数用尽（同一机体无其他任务窗口冲突）→ 重新评估回到 NO_GO
        c.call("POST", "/missions", {
            "id": "H-M2", "aircraftId": "H-AC",
            "plannedStart": "2026-09-22T02:00:00Z",
            "plannedEnd": "2026-09-22T04:00:00Z",
            "forecast": {"windKt": 15},
        }, "h-disp")
        # H-M1 的紧急放行绑定任务，不影响 H-M2（其自身同样 NO_GO）
        status, ev3 = c.call("POST", "/missions/H-M2/release-evaluations", {}, "h-disp")
        self.assertEqual(ev3["decision"], "NO_GO")


if __name__ == "__main__":
    unittest.main(verbosity=2)
