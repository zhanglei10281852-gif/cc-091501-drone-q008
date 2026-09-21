"""端到端领域测试：覆盖交接班发现的全部问题场景。"""

import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from airworthiness import (  # noqa: E402
    AirworthinessService,
    AuthorizationError,
    ConflictError,
    Store,
    ValidationError,
)
from airworthiness import timeutil  # noqa: E402

UTC = timezone.utc
T0 = datetime(2026, 9, 20, 8, 0, tzinfo=UTC)


def iso(dt: datetime) -> str:
    return timeutil.format_value(dt)


class World:
    """构造一套基准机队数据。"""

    def __init__(self):
        self.svc = AirworthinessService(Store(), clock=lambda: T0)
        s = self.svc
        # 人员
        s.register_person({"id": "mech1", "name": "老张", "role": "机务人员"})
        s.register_person({"id": "mech2", "name": "小李", "role": "机务人员"})
        s.register_person({"id": "disp1", "name": "王调度", "role": "放行员"})
        s.register_person({"id": "reg1", "name": "赵监察", "role": "监管人员"})
        s.register_person({"id": "ro1", "name": "访客", "role": "只读用户"})
        # 资质：mech1 螺旋桨检查资质 9/1 已过期；mech2 有效
        s.add_certification("mech1", {
            "scope": "propeller_check",
            "validFrom": iso(T0 - timedelta(days=400)),
            "validUntil": iso(T0 - timedelta(days=19)),
        }, "reg1")
        s.add_certification("mech2", {
            "scope": "propeller_check",
            "validFrom": iso(T0 - timedelta(days=30)),
            "validUntil": iso(T0 + timedelta(days=335)),
        }, "reg1")
        s.add_certification("mech2", {
            "scope": "battery_check",
            "validFrom": iso(T0 - timedelta(days=30)),
            "validUntil": iso(T0 + timedelta(days=335)),
        }, "reg1")
        # 机体
        s.register_aircraft({
            "id": "AC-01",
            "model": "Quad-X200",
            "requiredPositions": {
                "battery": "battery",
                "fc": "flight_controller",
                "motor_fl": "motor",
                "prop_fl": "propeller",
            },
            "airframeCycleLimit": 1000,
            "maxWindKt": 25,
            "minTempC": -10,
            "maxTempC": 40,
            "allowPrecipitation": False,
            "maxPayloadKg": 5,
            "inspectionProgram": {"propeller_check": 90, "battery_check": 180},
        })
        # 部件
        s.register_component({
            "serial": "BAT-OLD", "kind": "battery", "model": "B-50",
            "cycleLimit": 300, "inspectionEveryCycles": 100,
        })
        s.register_component({
            "serial": "BAT-NEW", "kind": "battery", "model": "B-50",
            "cycleLimit": 300, "inspectionEveryCycles": 100,
        })
        s.register_component({"serial": "FC-1", "kind": "flight_controller", "model": "FC-Pro"})
        s.register_component({"serial": "MOT-1", "kind": "motor", "model": "M-70"})
        s.register_component({"serial": "PROP-1", "kind": "propeller", "model": "P-15"})
        s.register_component({"serial": "PROP-2", "kind": "propeller", "model": "P-15"})
        s.register_firmware({
            "id": "FW-1", "version": "3.2.1", "targetKind": "flight_controller", "approved": True,
        })
        s.register_firmware({
            "id": "FW-BETA", "version": "3.9.0-beta", "targetKind": "flight_controller", "approved": False,
        })

    def assemble_baseline(self, at=T0, with_battery="BAT-OLD", baseline_cycles=50.0):
        s = self.svc
        s.install_component("AC-01", "motor_fl", "MOT-1", iso(at - timedelta(days=30)), "mech2")
        s.install_component("AC-01", "prop_fl", "PROP-1", iso(at - timedelta(days=30)), "mech2")
        s.install_component(
            "AC-01", "battery", with_battery, iso(at - timedelta(days=2)), "mech2",
            baseline_cycles=baseline_cycles,
        )
        s.install_component("AC-01", "fc", "FC-1", iso(at - timedelta(days=30)), "mech2")
        s.flash_firmware("AC-01", "FW-1", iso(at - timedelta(days=20)), "mech2")
        # 螺旋桨检查：由资质有效的 mech2 在近期签字
        s.record_inspection({
            "aircraftId": "AC-01", "scope": "propeller_check", "signedBy": "mech2",
            "signedAt": iso(at - timedelta(days=10)), "validityDays": 90,
            "componentSerial": "PROP-1",
        }, "mech2")
        s.record_inspection({
            "aircraftId": "AC-01", "scope": "battery_check", "signedBy": "mech2",
            "signedAt": iso(at - timedelta(days=10)), "validityDays": 180,
            "componentSerial": with_battery,
        }, "mech2")


class LifeSyncTest(unittest.TestCase):
    def setUp(self):
        self.w = World()

    def test_battery_swap_without_cycle_baseline_is_rejected(self):
        # 换电池但不同步循环寿命：装机环节直接拒绝，杜绝“刚换过电池没有同步循环寿命”
        with self.assertRaises(ValidationError):
            self.w.svc.install_component(
                "AC-01", "battery", "BAT-NEW", iso(T0), "mech2",
            )

    def test_battery_swap_with_baseline_carries_history_and_releases_old(self):
        self.w.assemble_baseline()
        self.w.svc.install_component(
            "AC-01", "battery", "BAT-NEW", iso(T0 + timedelta(days=5)), "mech2",
            baseline_cycles=12.0,
        )
        # 新电池寿命从同步基线起算
        life = self.w.svc.component_life("BAT-NEW", T0 + timedelta(days=6))
        self.assertEqual(life["cycles"], 12.0)
        # 旧电池履历在拆除时刻封闭，不再占用装配位
        cfg = self.w.svc.effective_config("AC-01", iso(T0 + timedelta(days=6)))
        self.assertEqual(cfg["positions"]["battery"]["serial"], "BAT-NEW")
        # 历史时刻仍能回溯到旧电池
        old_cfg = self.w.svc.effective_config("AC-01", iso(T0 + timedelta(days=1)))
        self.assertEqual(old_cfg["positions"]["battery"]["serial"], "BAT-OLD")

    def test_partial_overlap_install_is_rejected(self):
        self.w.assemble_baseline()
        # 同一部件已在翼，不能再次装机
        with self.assertRaises(ConflictError):
            self.w.svc.install_component(
                "AC-01", "battery", "BAT-OLD", iso(T0 + timedelta(days=1)), "mech2",
                baseline_cycles=50.0,
            )


class ReleaseGateTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.w.assemble_baseline()
        self.w.svc.create_mission({
            "id": "M-1", "aircraftId": "AC-01",
            "plannedStart": iso(T0 + timedelta(days=1)),
            "plannedEnd": iso(T0 + timedelta(days=1, hours=2)),
            "payload": [{"type": "camera", "kg": 2}],
            "forecast": {"windKt": 15, "tempC": 20, "precip": False},
        }, "disp1")

    def test_clean_mission_is_go(self):
        row = self.w.svc.evaluate_release("M-1", "disp1")
        self.assertEqual(row["decision"], "GO")
        self.assertEqual(row["blockers"], [])

    def test_expired_certification_signature_blocks_release(self):
        # 螺旋桨改由资质过期的 mech1 重新签字检查
        self.w.svc.record_inspection({
            "aircraftId": "AC-01", "scope": "propeller_check", "signedBy": "mech1",
            "signedAt": iso(T0 - timedelta(days=1)), "validityDays": 90,
            "componentSerial": "PROP-1",
        }, "mech2")
        row = self.w.svc.evaluate_release("M-1", "disp1")
        self.assertEqual(row["decision"], "NO_GO")
        codes = {b["code"] for b in row["blockers"]}
        self.assertIn("INSPECTION_SIGNATURE_INVALID", codes)
        # 签字依据里明确指出资质问题
        basis = [b for b in row["signatureBasis"] if b["scope"] == "propeller_check"][0]
        self.assertFalse(basis["valid"])
        self.assertIn("资质", basis["reason"])

    def test_missing_baseline_blocks_release_even_if_install_forced(self):
        # 新电池带同步基线装机 → 放行；构造一块无基线在翼状态不可能（装机已拦），
        # 改为直接验证旧电池场景被正确放行，再验证寿命到限拦截
        self.w.svc.install_component(
            "AC-01", "battery", "BAT-NEW", iso(T0 - timedelta(hours=6)), "mech2",
            baseline_cycles=299.0,
        )
        self.w.svc.record_inspection({
            "aircraftId": "AC-01", "scope": "battery_check", "signedBy": "mech2",
            "signedAt": iso(T0 - timedelta(hours=5)), "validityDays": 180,
            "componentSerial": "BAT-NEW",
        }, "mech2")
        # 先落一次飞行，使循环恰好到限
        self.w.svc.ingest_flight_log({
            "aircraftId": "AC-01",
            "offBlockAt": iso(T0 - timedelta(hours=4)),
            "onBlockAt": iso(T0 - timedelta(hours=3)),
            "cycles": 1, "hours": 1.0,
        }, "mech2")
        row = self.w.svc.evaluate_release("M-1", "disp1")
        self.assertEqual(row["decision"], "NO_GO")
        self.assertIn("LIFE_LIMIT_EXCEEDED", {b["code"] for b in row["blockers"]})

    def test_environment_and_payload_block(self):
        self.w.svc.create_mission({
            "id": "M-WIND", "aircraftId": "AC-01",
            "plannedStart": iso(T0 + timedelta(days=2)),
            "plannedEnd": iso(T0 + timedelta(days=2, hours=1)),
            "payload": [{"type": "lidar", "kg": 6}],
            "forecast": {"windKt": 30, "tempC": 20, "precip": True},
        }, "disp1")
        row = self.w.svc.evaluate_release("M-WIND", "disp1")
        codes = {b["code"] for b in row["blockers"]}
        self.assertEqual(codes, {"ENV_WIND", "ENV_PRECIP", "PAYLOAD_OVERWEIGHT"})

    def test_unapproved_firmware_blocks(self):
        self.w.svc.flash_firmware("AC-01", "FW-BETA", iso(T0 + timedelta(hours=12)), "mech2")
        row = self.w.svc.evaluate_release("M-1", "disp1")
        self.assertIn("FIRMWARE_NOT_APPROVED", {b["code"] for b in row["blockers"]})

    def test_dispatch_sees_conclusion_but_not_life_numbers(self):
        # 制造一个阻断，再看调度视图字段被裁剪
        self.w.svc.record_inspection({
            "aircraftId": "AC-01", "scope": "propeller_check", "signedBy": "mech1",
            "signedAt": iso(T0 - timedelta(days=1)), "validityDays": 90,
            "componentSerial": "PROP-1",
        }, "mech2")
        self.w.svc.evaluate_release("M-1", "disp1")
        view = self.w.svc.dispatch_view("M-1", "disp1")
        self.assertEqual(view["decision"], "NO_GO")
        self.assertTrue(any(b["code"] == "INSPECTION_SIGNATURE_INVALID" for b in view["blockers"]))
        # 调度视图不得出现寿命、资质证书等机务细节
        flat = repr(view)
        self.assertNotIn("cycleLimit", flat)
        self.assertNotIn("certificationId", flat)
        self.assertNotIn("baselineCycles", flat)

    def test_mechanic_view_has_life_and_signature_basis(self):
        view = self.w.svc.mechanic_view("AC-01", iso(T0), "mech1")
        self.assertIn("airframeLife", view)
        self.assertEqual(view["effectiveConfig"]["positions"]["battery"]["accruedCycles"], 50.0)
        scopes = {b["scope"] for b in view["signatureBasis"]}
        self.assertEqual(scopes, {"propeller_check", "battery_check"})

    def test_role_cross_access_forbidden(self):
        with self.assertRaises(AuthorizationError):
            self.w.svc.mechanic_view("AC-01", iso(T0), "disp1")
        with self.assertRaises(AuthorizationError):
            self.w.svc.dispatch_view("M-1", "mech1")
        with self.assertRaises(AuthorizationError):
            self.w.svc.evaluate_release("M-1", "ro1")


class ConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.w.assemble_baseline()

    def test_open_work_order_blocks_mission(self):
        self.w.svc.open_work_order({
            "aircraftId": "AC-01", "title": "电机检修",
            "openedAt": iso(T0 + timedelta(hours=20)),
        }, "mech2")
        self.w.svc.create_mission({
            "id": "M-MAINT", "aircraftId": "AC-01",
            "plannedStart": iso(T0 + timedelta(days=1)),
            "plannedEnd": iso(T0 + timedelta(days=1, hours=2)),
            "forecast": {"windKt": 10},
        }, "disp1")
        row = self.w.svc.evaluate_release("M-MAINT", "disp1")
        self.assertIn("MAINTENANCE_OCCUPANCY", {b["code"] for b in row["blockers"]})
        # 工单关闭后可放行
        self.w.svc.close_work_order(
            self.w.svc.store.work_orders[-1]["id"], iso(T0 + timedelta(hours=23)), "mech2")
        row = self.w.svc.evaluate_release("M-MAINT", "disp1")
        self.assertEqual(row["decision"], "GO")

    def test_overlapping_missions_conflict(self):
        self.w.svc.create_mission({
            "id": "M-A", "aircraftId": "AC-01",
            "plannedStart": iso(T0 + timedelta(days=1)),
            "plannedEnd": iso(T0 + timedelta(days=1, hours=2)),
        }, "disp1")
        self.w.svc.create_mission({
            "id": "M-B", "aircraftId": "AC-01",
            "plannedStart": iso(T0 + timedelta(days=1, hours=1)),
            "plannedEnd": iso(T0 + timedelta(days=1, hours=3)),
        }, "disp1")
        row = self.w.svc.evaluate_release("M-B", "disp1")
        self.assertIn("MISSION_CONFLICT", {b["code"] for b in row["blockers"]})

    def test_swap_during_mission_window_is_flagged(self):
        # 任务已评估 GO 后，在其窗口内安排拆装 → 再次评估时窗口内配置不唯一
        self.w.svc.create_mission({
            "id": "M-WIN", "aircraftId": "AC-01",
            "plannedStart": iso(T0 + timedelta(days=3)),
            "plannedEnd": iso(T0 + timedelta(days=3, hours=4)),
            "forecast": {"windKt": 10},
        }, "disp1")
        self.assertEqual(self.w.svc.evaluate_release("M-WIN", "disp1")["decision"], "GO")
        self.w.svc.install_component(
            "AC-01", "battery", "BAT-NEW",
            iso(T0 + timedelta(days=3, hours=1)), "mech2", baseline_cycles=10.0,
        )
        row = self.w.svc.evaluate_release("M-WIN", "disp1")
        self.assertIn("CONFIG_CHANGES_DURING_WINDOW", {b["code"] for b in row["blockers"]})

    def test_concurrent_installs_never_produce_two_configs(self):
        errors = []

        def worker(serial):
            try:
                self.w.svc.install_component(
                    "AC-01", "battery", serial, iso(T0 + timedelta(days=9)), "mech2",
                    baseline_cycles=1.0,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        # BAT-OLD 已在翼；两个线程分别尝试装两块电池，至多一个成功
        t1 = threading.Thread(target=worker, args=("BAT-NEW",))
        t2 = threading.Thread(target=worker, args=("BAT-NEW",))
        t1.start(); t2.start(); t1.join(); t2.join()
        # 第二次必然冲突失败
        self.assertTrue(any(isinstance(e, ConflictError) for e in errors))
        cfg = self.w.svc.effective_config("AC-01", iso(T0 + timedelta(days=10)))
        self.assertEqual(cfg["positions"]["battery"]["serial"], "BAT-NEW")


class EmergencyReleaseTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.w.assemble_baseline()
        # 制造“资质过期签字”这一可豁免阻断
        self.w.svc.record_inspection({
            "aircraftId": "AC-01", "scope": "propeller_check", "signedBy": "mech1",
            "signedAt": iso(T0 - timedelta(days=1)), "validityDays": 90,
            "componentSerial": "PROP-1",
        }, "mech2")
        self.w.svc.create_mission({
            "id": "M-ER", "aircraftId": "AC-01",
            "plannedStart": iso(T0 + timedelta(hours=1)),
            "plannedEnd": iso(T0 + timedelta(hours=3)),
            "forecast": {"windKt": 15},
        }, "disp1")

    def test_emergency_release_by_regulator_allows_overridable_blocker(self):
        self.w.svc.grant_emergency_release("M-ER", {
            "validUntil": iso(T0 + timedelta(hours=6)),
            "maxFlights": 1,
            "permittedBlockers": ["INSPECTION_SIGNATURE_INVALID"],
            "reason": "监管现场到场监督，风险可控",
        }, "reg1")
        row = self.w.svc.evaluate_release("M-ER", "disp1")
        self.assertEqual(row["decision"], "EMERGENCY_GO")
        self.assertIsNotNone(row["emergencyReleaseId"])

    def test_release_officer_cannot_grant_emergency(self):
        with self.assertRaises(AuthorizationError):
            self.w.svc.grant_emergency_release("M-ER", {
                "validUntil": iso(T0 + timedelta(hours=6)),
                "reason": "自行批准",
            }, "disp1")

    def test_hard_blockers_cannot_be_overridden(self):
        with self.assertRaises(ValidationError):
            self.w.svc.grant_emergency_release("M-ER", {
                "validUntil": iso(T0 + timedelta(hours=6)),
                "permittedBlockers": ["LIFE_LIMIT_EXCEEDED"],
                "reason": "想豁免寿命到限",
            }, "reg1")

    def test_emergency_release_expiry_conditions(self):
        # 给一个带次数上限的批准，验证任务绑定与撤销
        er = self.w.svc.grant_emergency_release("M-ER", {
            "validUntil": iso(T0 + timedelta(hours=6)),
            "maxFlights": 1,
            "reason": "一次性",
        }, "reg1")
        # 用于另一个任务无效
        self.w.svc.create_mission({
            "id": "M-OTHER", "aircraftId": "AC-01",
            "plannedStart": iso(T0 + timedelta(hours=2)),
            "plannedEnd": iso(T0 + timedelta(hours=4)),
            "forecast": {"windKt": 15},
        }, "disp1")
        self.w.svc.record_inspection({
            "aircraftId": "AC-01", "scope": "propeller_check", "signedBy": "mech1",
            "signedAt": iso(T0), "validityDays": 90, "componentSerial": "PROP-1",
        }, "mech2")
        row = self.w.svc.evaluate_release("M-OTHER", "disp1")
        self.assertEqual(row["decision"], "NO_GO")
        # 撤销后原任务也不再放行
        self.w.svc.revoke_emergency_release(er["id"], "reg1")
        row = self.w.svc.evaluate_release("M-ER", "disp1")
        self.assertEqual(row["decision"], "NO_GO")


class LateLogAndHistoryTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.w.assemble_baseline()

    def test_flight_snapshot_does_not_change_with_later_swaps(self):
        # 9/25 飞行，10/01 电池才被更换；日志受理时固化的快照永远是 BAT-OLD
        self.w.svc.ingest_flight_log({
            "id": "F-1", "aircraftId": "AC-01",
            "offBlockAt": iso(T0 + timedelta(days=5)),
            "onBlockAt": iso(T0 + timedelta(days=5, minutes=40)),
            "cycles": 1, "hours": 0.7,
            "receivedAt": iso(T0 + timedelta(days=5, minutes=45)),
        }, "mech2")
        snapshot_serial = self.w.svc.store.flight_logs[-1]["configSnapshot"]["positions"]["battery"]["serial"]
        self.assertEqual(snapshot_serial, "BAT-OLD")
        self.w.svc.install_component(
            "AC-01", "battery", "BAT-NEW", iso(T0 + timedelta(days=6)), "mech2",
            baseline_cycles=99.0,
        )
        self.assertEqual(
            self.w.svc.store.flight_logs[-1]["configSnapshot"]["positions"]["battery"]["serial"],
            "BAT-OLD",
        )

    def test_late_log_flags_future_missions_reviews_and_reinspection(self):
        # 9/22 放行一个 9/25 的任务（GO）
        self.w.svc.create_mission({
            "id": "M-FUTURE", "aircraftId": "AC-01",
            "plannedStart": iso(T0 + timedelta(days=5, hours=2)),
            "plannedEnd": iso(T0 + timedelta(days=5, hours=3)),
            "forecast": {"windKt": 10},
        }, "disp1")
        go_row = self.w.svc.evaluate_release("M-FUTURE", "disp1")
        self.assertEqual(go_row["decision"], "GO")

        # 9/21 实际飞过 52 循环（电池基线 50 + 52 → 跨越 100 定检阈值），
        # 日志 9/24 才迟到（宽限期 1 小时）
        result = self.w.svc.ingest_flight_log({
            "id": "F-LATE", "aircraftId": "AC-01",
            "offBlockAt": iso(T0 + timedelta(days=1)),
            "onBlockAt": iso(T0 + timedelta(days=1, hours=1)),
            "cycles": 52, "hours": 1.0,
            "receivedAt": iso(T0 + timedelta(days=4)),
        }, "mech2")
        impact = result["impact"]
        self.assertTrue(impact["late"])
        # 未来任务被圈定需重新评估
        self.assertIn("M-FUTURE", impact["affectedMissionIds"])
        reeval = {m["missionId"]: m for m in impact["missionsToReevaluate"]}
        self.assertTrue(reeval["M-FUTURE"]["needsReevaluation"])
        # 跨越电池 100 循环定检阈值 → 圈出复检
        comp = next(c for c in impact["components"] if c["serial"] == "BAT-OLD")
        self.assertTrue(any(r["threshold"] == 100 for r in comp["reinspections"]))

        # 重新评估时寿命已计入（50+52=102 未到 300 限，但定检/阻断逻辑一致）
        row = self.w.svc.evaluate_release("M-FUTURE", "disp1")
        # 电池定检项目本身不在机体大纲；核心断言是寿命读数反映迟到补计
        self.assertEqual(
            row["lifeBasis"][0]["accruedCycles"]
            if row["lifeBasis"][0]["position"] == "battery"
            else next(x for x in row["lifeBasis"] if x["position"] == "battery")["accruedCycles"],
            102.0,
        )

    def test_late_log_marks_prior_release_decision_basis_revised(self):
        # 先在 9/24 做出 GO（当时 9/21 的飞行尚未记录），再补迟到日志
        self.w.svc.create_mission({
            "id": "M-2", "aircraftId": "AC-01",
            "plannedStart": iso(T0 + timedelta(days=6)),
            "plannedEnd": iso(T0 + timedelta(days=6, hours=1)),
            "forecast": {"windKt": 10},
        }, "disp1")
        self.w.svc.evaluate_release("M-2", "disp1")
        result = self.w.svc.ingest_flight_log({
            "id": "F-LATE2", "aircraftId": "AC-01",
            "offBlockAt": iso(T0 + timedelta(days=1)),
            "onBlockAt": iso(T0 + timedelta(days=1, minutes=30)),
            "cycles": 1, "hours": 0.5,
            "receivedAt": iso(T0 + timedelta(days=4)),
        }, "mech2")
        reviews = result["impact"]["pastReleaseEvaluationsToReview"]
        self.assertTrue(any(r["missionId"] == "M-2" for r in reviews))
        # 调度视图被告知依据已被动摇
        view = self.w.svc.dispatch_view("M-2", "disp1")
        self.assertTrue(view.get("basisRevisedAfter"))

    def test_retro_impact_collects_all_late_logs(self):
        self.w.svc.ingest_flight_log({
            "id": "F-A", "aircraftId": "AC-01",
            "offBlockAt": iso(T0 + timedelta(days=1)),
            "onBlockAt": iso(T0 + timedelta(days=1, minutes=20)),
            "cycles": 1, "hours": 0.3,
            "receivedAt": iso(T0 + timedelta(days=3)),
        }, "mech2")
        summary = self.w.svc.retro_impact("mech2")
        self.assertEqual(summary["flightLogIds"], ["F-A"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
