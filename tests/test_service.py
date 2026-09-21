import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from airworthiness.errors import (  # noqa: E402
    ConflictError,
    ForbiddenError,
    ReleaseBlocked,
    ValidationError,
)
from airworthiness.service import AirworthinessService  # noqa: E402
from airworthiness.store import Store  # noqa: E402

# 统一时间线（Asia/Shanghai）
T_REG = "2026-09-21T08:00:00+08:00"
T_INSTALL = "2026-09-21T09:00:00+08:00"
T_SIGN = "2026-09-21T09:30:00+08:00"
T_M0_START = "2026-09-21T10:00:00+08:00"
T_M0_END = "2026-09-21T10:30:00+08:00"
T_M1_START = "2026-09-21T11:00:00+08:00"
T_M1_END = "2026-09-21T11:30:00+08:00"
T_M2_START = "2026-09-21T12:00:00+08:00"
T_M2_END = "2026-09-21T12:30:00+08:00"
T_M3_START = "2026-09-21T14:00:00+08:00"
T_M3_END = "2026-09-21T14:30:00+08:00"
T_NEXT_DAY = "2026-09-22T09:00:00+08:00"
QUAL_UNTIL = "2026-12-31T23:59:59+08:00"
SIG_UNTIL = "2026-09-23T09:30:00+08:00"

MAINT = "maintenance"
DISPATCH = "dispatcher"
ENGINEER = "airworthiness_engineer"


def make_service():
    return AirworthinessService(Store())


def register_airframe(svc, airframe_id="AF-1", **overrides):
    body = {
        "id": airframe_id,
        "model": "quad-x",
        "serial": f"SN-{airframe_id}",
        "positions": {"battery-1": "battery", "propeller-1": "propeller"},
        "approved_firmware": ["fw-1.0", "fw-1.1"],
        "required_inspections": ["preflight", "propeller"],
        "compatible_payloads": ["camera"],
        "max_payload_kg": 2.0,
        "max_wind_mps": 12.0,
        "min_temp_c": -10.0,
        "max_temp_c": 40.0,
        "allow_precipitation": False,
    }
    body.update(overrides)
    return svc.register_airframe(body, role=MAINT)


def register_part(svc, serial, part_type, synced=True, **overrides):
    body = {
        "serial": serial,
        "type": part_type,
        "model": "m1",
        "cycle_limit": 100,
        "hour_limit": 50.0,
        "inspection_interval_cycles": 40,
        "used_cycles": 0,
        "used_hours": 0.0,
    }
    if synced:
        body["life_synced_at"] = T_REG
    body.update(overrides)
    return svc.register_part(body, role=MAINT)


def qualify(svc, personnel="张工", kinds=("preflight", "propeller"), valid_until=QUAL_UNTIL):
    for kind in kinds:
        svc.grant_qualification(
            {
                "personnel_id": personnel,
                "kind": kind,
                "valid_from": T_REG,
                "valid_until": valid_until,
            },
            role=MAINT,
        )


def sign(svc, airframe_id, kind, personnel="张工", signed_at=T_SIGN, result="pass"):
    return svc.sign_inspection(
        airframe_id,
        {
            "inspection_kind": kind,
            "personnel_id": personnel,
            "signed_at": signed_at,
            "result": result,
            "valid_until": SIG_UNTIL,
        },
        role=MAINT,
    )


def ready_airframe(svc, airframe_id="AF-1", battery="BAT-1", propeller="PROP-1", battery_synced=True):
    register_airframe(svc, airframe_id)
    register_part(svc, battery, "battery", synced=battery_synced)
    register_part(svc, propeller, "propeller")
    svc.install_part(airframe_id, {"part_serial": battery, "position": "battery-1", "at": T_INSTALL}, role=MAINT)
    svc.install_part(airframe_id, {"part_serial": propeller, "position": "propeller-1", "at": T_INSTALL}, role=MAINT)
    svc.activate_firmware(airframe_id, {"version": "fw-1.1", "at": T_INSTALL}, role=MAINT)
    qualify(svc)
    sign(svc, airframe_id, "preflight")
    sign(svc, airframe_id, "propeller")


def create_mission(svc, mission_id="M-1", airframe_id="AF-1", start=T_M1_START, end=T_M1_END, **overrides):
    body = {
        "id": mission_id,
        "airframe_id": airframe_id,
        "payload": {"kind": "camera", "weight_kg": 1.0},
        "planned_start": start,
        "planned_end": end,
        "environment": {"wind_mps": 5.0, "temp_c": 20.0, "precipitation": False},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(body.get(key), dict):
            body[key].update(value)
        else:
            body[key] = value
    return svc.create_mission(body, role=DISPATCH)


def blocker_codes(decision):
    return {b.code for b in decision.blockers}


class ReleaseEvaluationTest(unittest.TestCase):
    def test_battery_life_unsynced_blocks_release_until_synced(self):
        svc = make_service()
        ready_airframe(svc, battery_synced=False)
        create_mission(svc)

        decision = svc.evaluate_mission("M-1", role=DISPATCH)
        self.assertEqual(decision.status, "blocked")
        self.assertIn("PART_LIFE_UNSYNCED", blocker_codes(decision))

        with self.assertRaises(ReleaseBlocked):
            svc.dispatch_mission("M-1", role=DISPATCH)
        self.assertEqual(svc.store.missions["M-1"].status, "scheduled")

        svc.sync_part_life("BAT-1", {"used_cycles": 12, "used_hours": 6.0, "at": T_SIGN}, role=MAINT)
        decision = svc.evaluate_mission("M-1", role=DISPATCH)
        self.assertEqual(decision.status, "released")
        self.assertEqual(decision.via, "standard")

    def test_expired_qualification_signature_blocks_release(self):
        svc = make_service()
        register_airframe(svc)
        register_part(svc, "BAT-1", "battery")
        register_part(svc, "PROP-1", "propeller")
        svc.install_part("AF-1", {"part_serial": "BAT-1", "position": "battery-1", "at": T_INSTALL}, role=MAINT)
        svc.install_part("AF-1", {"part_serial": "PROP-1", "position": "propeller-1", "at": T_INSTALL}, role=MAINT)
        svc.activate_firmware("AF-1", {"version": "fw-1.1", "at": T_INSTALL}, role=MAINT)
        qualify(svc, kinds=("preflight",))
        # 螺旋桨检查由资质已过期人员签署
        svc.grant_qualification(
            {
                "personnel_id": "李工",
                "kind": "propeller",
                "valid_from": "2025-01-01T00:00:00+08:00",
                "valid_until": "2026-01-01T00:00:00+08:00",
            },
            role=MAINT,
        )
        sign(svc, "AF-1", "preflight")
        prop_sig = sign(svc, "AF-1", "propeller", personnel="李工")
        self.assertFalse(prop_sig["qualification_valid"])

        create_mission(svc)
        decision = svc.evaluate_mission("M-1", role=DISPATCH)
        self.assertEqual(decision.status, "blocked")
        self.assertIn("INSPECTION_SIGNATURE_INVALID", blocker_codes(decision))

        # 由资质有效人员重签后放行
        qualify(svc, personnel="王工", kinds=("propeller",))
        sign(svc, "AF-1", "propeller", personnel="王工", signed_at="2026-09-21T09:45:00+08:00")
        decision = svc.evaluate_mission("M-1", role=DISPATCH)
        self.assertEqual(decision.status, "released")

    def test_environment_payload_and_firmware_blockers(self):
        svc = make_service()
        ready_airframe(svc)
        create_mission(svc, "M-WIND", environment={"wind_mps": 20.0})
        create_mission(svc, "M-HEAVY", payload={"weight_kg": 5.0})
        create_mission(svc, "M-PAYLOAD", payload={"kind": "sprayer"})
        create_mission(svc, "M-RAIN", environment={"precipitation": True})
        create_mission(svc, "M-COLD", environment={"temp_c": -30.0})

        self.assertIn("ENV_WIND_EXCEEDED", blocker_codes(svc.evaluate_mission("M-WIND", role=DISPATCH)))
        self.assertIn("PAYLOAD_OVERWEIGHT", blocker_codes(svc.evaluate_mission("M-HEAVY", role=DISPATCH)))
        self.assertIn("PAYLOAD_INCOMPATIBLE", blocker_codes(svc.evaluate_mission("M-PAYLOAD", role=DISPATCH)))
        self.assertIn("ENV_PRECIPITATION", blocker_codes(svc.evaluate_mission("M-RAIN", role=DISPATCH)))
        self.assertIn("ENV_TEMP_OUT_OF_RANGE", blocker_codes(svc.evaluate_mission("M-COLD", role=DISPATCH)))

        svc.activate_firmware("AF-1", {"version": "fw-9.9", "at": T_SIGN}, role=MAINT)
        create_mission(svc, "M-FW")
        self.assertIn("FIRMWARE_NOT_APPROVED", blocker_codes(svc.evaluate_mission("M-FW", role=DISPATCH)))

    def test_life_exceeded_blocks_and_is_not_overridable(self):
        svc = make_service()
        ready_airframe(svc)
        svc.sync_part_life("BAT-1", {"used_cycles": 100, "used_hours": 6.0, "at": T_SIGN}, role=MAINT)
        create_mission(svc)
        decision = svc.evaluate_mission("M-1", role=DISPATCH)
        self.assertIn("PART_LIFE_EXCEEDED", blocker_codes(decision))

        svc.approve_emergency_release(
            {
                "id": "ER-1",
                "airframe_id": "AF-1",
                "requested_by": "调度甲",
                "approved_by": "总工",
                "approved_at": T_SIGN,
                "expires_at": T_NEXT_DAY,
                "overrides": ["PART_LIFE_UNSYNCED", "INSPECTION_MISSING"],
                "conditions": "仅限本场调机",
            },
            role=ENGINEER,
        )
        decision = svc.evaluate_mission("M-1", role=DISPATCH)
        self.assertEqual(decision.status, "blocked")
        self.assertEqual(decision.emergency["valid"], False)


class InstallHistoryTest(unittest.TestCase):
    def test_install_remove_chain_is_continuous(self):
        svc = make_service()
        register_airframe(svc)
        register_part(svc, "BAT-1", "battery")
        register_part(svc, "BAT-2", "battery")
        svc.install_part("AF-1", {"part_serial": "BAT-1", "position": "battery-1", "at": T_INSTALL}, role=MAINT)

        with self.assertRaises(ConflictError):
            svc.install_part("AF-1", {"part_serial": "BAT-2", "position": "battery-1", "at": T_SIGN}, role=MAINT)

        svc.remove_part("AF-1", {"part_serial": "BAT-1", "at": T_M0_START}, role=MAINT)
        svc.install_part("AF-1", {"part_serial": "BAT-2", "position": "battery-1", "at": T_M0_END}, role=MAINT)

        history = svc.part_history("AF-1", role=MAINT)["events"]
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["part_serial"], "BAT-1")
        self.assertIsNotNone(history[0]["removed_at"])
        self.assertEqual(history[1]["part_serial"], "BAT-2")
        self.assertIsNone(history[1]["removed_at"])

    def test_part_cannot_be_on_two_airframes_and_no_backdating(self):
        svc = make_service()
        register_airframe(svc, "AF-1")
        register_airframe(svc, "AF-2")
        register_part(svc, "BAT-1", "battery")
        svc.install_part("AF-1", {"part_serial": "BAT-1", "position": "battery-1", "at": T_INSTALL}, role=MAINT)

        with self.assertRaises(ConflictError):
            svc.install_part("AF-2", {"part_serial": "BAT-1", "position": "battery-1", "at": T_SIGN}, role=MAINT)

        svc.remove_part("AF-1", {"part_serial": "BAT-1", "at": T_M0_START}, role=MAINT)
        with self.assertRaises(ValidationError):
            svc.install_part("AF-2", {"part_serial": "BAT-1", "position": "battery-1", "at": T_REG}, role=MAINT)

    def test_wrong_position_and_type_rejected(self):
        svc = make_service()
        register_airframe(svc)
        register_part(svc, "BAT-1", "battery")
        with self.assertRaises(ValidationError):
            svc.install_part("AF-1", {"part_serial": "BAT-1", "position": "propeller-1", "at": T_INSTALL}, role=MAINT)
        with self.assertRaises(ValidationError):
            svc.install_part("AF-1", {"part_serial": "BAT-1", "position": "motor-9", "at": T_INSTALL}, role=MAINT)


class SnapshotImmutabilityTest(unittest.TestCase):
    def test_historical_flight_config_immune_to_current_assembly(self):
        svc = make_service()
        ready_airframe(svc)
        create_mission(svc)
        decision = svc.dispatch_mission("M-1", role=DISPATCH)
        config_id = decision.config_id
        snapshot_before = svc.store.snapshots[config_id].to_dict()

        svc.ingest_flight_log(
            {"id": "L-1", "mission_id": "M-1", "started_at": T_M1_START, "ended_at": T_M1_END,
             "cycles": 2, "hours": 0.5},
            role=MAINT,
        )
        # 当前装配变更：拆旧装新、升级固件
        svc.remove_part("AF-1", {"part_serial": "BAT-1", "at": T_NEXT_DAY}, role=MAINT)
        register_part(svc, "BAT-2", "battery")
        svc.install_part("AF-1", {"part_serial": "BAT-2", "position": "battery-1", "at": T_NEXT_DAY}, role=MAINT)
        svc.activate_firmware("AF-1", {"version": "fw-1.0", "at": T_NEXT_DAY}, role=MAINT)

        snapshot_after = svc.store.snapshots[config_id].to_dict()
        self.assertEqual(snapshot_before, snapshot_after)
        self.assertEqual(svc.store.flight_logs["L-1"].config_id, config_id)
        self.assertEqual(
            {p.serial for p in svc.store.snapshots[config_id].parts},
            {"BAT-1", "PROP-1"},
        )
        # 指定时刻配置：历史时刻仍看到旧装配，当前时刻看到新装配
        from airworthiness.timeutil import parse_ts

        past = svc.effective_config_at("AF-1", parse_ts(T_M1_START))
        now = svc.effective_config_at("AF-1", parse_ts("2026-09-22T10:00:00+08:00"))
        self.assertEqual(past["parts"]["battery-1"].serial, "BAT-1")
        self.assertEqual(now["parts"]["battery-1"].serial, "BAT-2")
        self.assertEqual(past["firmware_version"], "fw-1.1")
        self.assertEqual(now["firmware_version"], "fw-1.0")


class OccupancyTest(unittest.TestCase):
    def test_maintenance_and_mission_are_mutually_exclusive(self):
        svc = make_service()
        ready_airframe(svc)
        svc.open_work_order(
            {"id": "WO-1", "airframe_id": "AF-1", "kind": "定检", "grounding": True, "opened_at": T_SIGN},
            role=MAINT,
        )
        create_mission(svc)
        decision = svc.evaluate_mission("M-1", role=DISPATCH)
        self.assertIn("WORK_ORDER_OPEN", blocker_codes(decision))
        self.assertIn("OCCUPANCY_CONFLICT", blocker_codes(decision))
        with self.assertRaises(ReleaseBlocked):
            svc.dispatch_mission("M-1", role=DISPATCH)

        svc.close_work_order("WO-1", {"closed_at": T_M0_END}, role=MAINT)
        decision = svc.dispatch_mission("M-1", role=DISPATCH)
        self.assertEqual(decision.status, "released")

        # 任务占用期间禁止开立停场工单、禁止变更装配
        with self.assertRaises(ConflictError):
            svc.open_work_order(
                {"id": "WO-2", "airframe_id": "AF-1", "kind": "排故", "grounding": True, "opened_at": T_M1_START},
                role=MAINT,
            )
        with self.assertRaises(ConflictError):
            svc.remove_part("AF-1", {"part_serial": "BAT-1", "at": T_M1_START}, role=MAINT)

    def test_overlapping_missions_conflict(self):
        svc = make_service()
        ready_airframe(svc)
        create_mission(svc, "M-1")
        create_mission(svc, "M-2", start="2026-09-21T11:15:00+08:00", end="2026-09-21T12:00:00+08:00")
        svc.dispatch_mission("M-1", role=DISPATCH)
        with self.assertRaises(ReleaseBlocked) as ctx:
            svc.dispatch_mission("M-2", role=DISPATCH)
        self.assertIn("OCCUPANCY_CONFLICT", blocker_codes(ctx.exception.decision))

    def test_concurrent_dispatch_yields_single_effective_config(self):
        svc = make_service()
        ready_airframe(svc)
        create_mission(svc, "M-1")
        create_mission(svc, "M-2", start="2026-09-21T11:15:00+08:00", end="2026-09-21T12:00:00+08:00")

        outcomes = []

        def race(mission_id):
            try:
                svc.dispatch_mission(mission_id, role=DISPATCH)
                outcomes.append("released")
            except ReleaseBlocked:
                outcomes.append("blocked")

        threads = [threading.Thread(target=race, args=(mid,)) for mid in ("M-1", "M-2")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sorted(outcomes), ["blocked", "released"])
        mission_occupancies = [o for o in svc.store.occupancies if o.kind == "mission"]
        self.assertEqual(len(mission_occupancies), 1)
        self.assertEqual(len(svc.store.snapshots), 1)


class EmergencyReleaseTest(unittest.TestCase):
    def _blocked_by_unsynced(self):
        svc = make_service()
        ready_airframe(svc, battery_synced=False)
        create_mission(svc)
        return svc

    def _approve(self, svc, **overrides):
        body = {
            "id": "ER-1",
            "airframe_id": "AF-1",
            "mission_id": "M-1",
            "requested_by": "调度甲",
            "approved_by": "总工",
            "approved_at": T_SIGN,
            "expires_at": T_NEXT_DAY,
            "overrides": ["PART_LIFE_UNSYNCED"],
            "conditions": "仅限 M-1 单次任务，落地后复测",
        }
        body.update(overrides)
        return svc.approve_emergency_release(body, role=ENGINEER)

    def test_emergency_release_allows_dispatch_until_expiry(self):
        svc = self._blocked_by_unsynced()
        self._approve(svc)
        decision = svc.evaluate_mission("M-1", role=DISPATCH)
        self.assertEqual(decision.status, "released")
        self.assertEqual(decision.via, "emergency")
        self.assertEqual(decision.emergency["expires_at"], T_NEXT_DAY)
        decision = svc.dispatch_mission("M-1", role=DISPATCH)
        self.assertEqual(decision.via, "emergency")

    def test_emergency_release_expired_or_wrong_mission(self):
        svc = self._blocked_by_unsynced()
        self._approve(svc, expires_at="2026-09-21T10:00:00+08:00")
        decision = svc.evaluate_mission("M-1", role=DISPATCH)
        self.assertEqual(decision.status, "blocked")
        self.assertIn("已过失效时间", svc.evaluate_mission("M-1", role=DISPATCH).emergency["reason"])

        create_mission(svc, "M-2", start=T_M2_START, end=T_M2_END)
        decision = svc.evaluate_mission("M-2", role=DISPATCH)
        self.assertEqual(decision.status, "blocked")

    def test_emergency_independence_rules(self):
        svc = self._blocked_by_unsynced()
        with self.assertRaises(ForbiddenError):
            self._approve(svc, approved_by="调度甲")
        with self.assertRaises(ForbiddenError):
            svc.approve_emergency_release(
                {
                    "id": "ER-X",
                    "airframe_id": "AF-1",
                    "requested_by": "调度甲",
                    "approved_by": "调度乙",
                    "approved_at": T_SIGN,
                    "expires_at": T_NEXT_DAY,
                    "overrides": ["PART_LIFE_UNSYNCED"],
                    "conditions": "测试",
                },
                role=MAINT,
            )
        with self.assertRaises(ValidationError):
            self._approve(svc, id="ER-Y", overrides=["PART_LIFE_EXCEEDED"])

    def test_emergency_approver_must_not_be_involved_signer(self):
        svc = make_service()
        register_airframe(svc)
        register_part(svc, "BAT-1", "battery")
        register_part(svc, "PROP-1", "propeller")
        svc.install_part("AF-1", {"part_serial": "BAT-1", "position": "battery-1", "at": T_INSTALL}, role=MAINT)
        svc.install_part("AF-1", {"part_serial": "PROP-1", "position": "propeller-1", "at": T_INSTALL}, role=MAINT)
        svc.activate_firmware("AF-1", {"version": "fw-1.1", "at": T_INSTALL}, role=MAINT)
        qualify(svc, kinds=("preflight",))
        svc.grant_qualification(
            {"personnel_id": "李工", "kind": "propeller",
             "valid_from": "2025-01-01T00:00:00+08:00", "valid_until": "2026-01-01T00:00:00+08:00"},
            role=MAINT,
        )
        sign(svc, "AF-1", "preflight")
        sign(svc, "AF-1", "propeller", personnel="李工")
        create_mission(svc)
        # 李工是资质失效的涉事签字人，不能批准覆盖该阻断的紧急放行
        with self.assertRaises(ForbiddenError):
            svc.approve_emergency_release(
                {
                    "id": "ER-1",
                    "airframe_id": "AF-1",
                    "requested_by": "调度甲",
                    "approved_by": "李工",
                    "approved_at": T_SIGN,
                    "expires_at": T_NEXT_DAY,
                    "overrides": ["INSPECTION_SIGNATURE_INVALID"],
                    "conditions": "测试",
                },
                role=ENGINEER,
            )

    def test_emergency_cycle_limit_exhaustion(self):
        svc = self._blocked_by_unsynced()
        self._approve(svc, max_additional_cycles=2)
        svc.dispatch_mission("M-1", role=DISPATCH)
        svc.ingest_flight_log(
            {"id": "L-1", "mission_id": "M-1", "started_at": T_M1_START, "ended_at": T_M1_END,
             "cycles": 2, "hours": 0.5},
            role=MAINT,
        )
        create_mission(svc, "M-2", start=T_M2_START, end=T_M2_END)
        self._approve(svc, id="ER-2", mission_id="M-2", max_additional_cycles=2)
        decision = svc.evaluate_mission("M-2", role=DISPATCH)
        self.assertEqual(decision.status, "blocked")
        self.assertIn("附加循环已用尽", decision.emergency["reason"])


class LateFlightLogImpactTest(unittest.TestCase):
    def test_late_log_backfills_life_and_flags_impacted_scope(self):
        svc = make_service()
        register_airframe(svc)
        register_part(svc, "BAT-1", "battery", cycle_limit=10, inspection_interval_cycles=4)
        register_part(svc, "PROP-1", "propeller", inspection_interval_cycles=None)
        svc.install_part("AF-1", {"part_serial": "BAT-1", "position": "battery-1", "at": T_INSTALL}, role=MAINT)
        svc.install_part("AF-1", {"part_serial": "PROP-1", "position": "propeller-1", "at": T_INSTALL}, role=MAINT)
        svc.activate_firmware("AF-1", {"version": "fw-1.1", "at": T_INSTALL}, role=MAINT)
        qualify(svc)
        sign(svc, "AF-1", "preflight")
        sign(svc, "AF-1", "propeller")

        create_mission(svc, "M-1", start=T_M1_START, end=T_M1_END)
        svc.dispatch_mission("M-1", role=DISPATCH)
        svc.ingest_flight_log(
            {"id": "L-1", "mission_id": "M-1", "started_at": T_M1_START, "ended_at": T_M1_END,
             "cycles": 3, "hours": 0.5},
            role=MAINT,
        )
        create_mission(svc, "M-2", start=T_M2_START, end=T_M2_END)
        svc.dispatch_mission("M-2", role=DISPATCH)
        svc.ingest_flight_log(
            {"id": "L-2", "mission_id": "M-2", "started_at": T_M2_START, "ended_at": T_M2_END,
             "cycles": 3, "hours": 0.5},
            role=MAINT,
        )
        create_mission(svc, "M-3", start=T_M3_START, end=T_M3_END)

        # 迟到的更早航班：M-0 在 M-1 之前飞，日志现在才到
        create_mission(svc, "M-0", start=T_M0_START, end=T_M0_END)
        svc.dispatch_mission("M-0", role=DISPATCH)
        report = svc.ingest_flight_log(
            {"id": "L-0", "mission_id": "M-0", "started_at": T_M0_START, "ended_at": T_M0_END,
             "cycles": 5, "hours": 0.5},
            role=MAINT,
        )

        self.assertTrue(report["late"])
        self.assertEqual(svc.store.parts["BAT-1"].used_cycles, 11)

        findings = {entry["mission_id"]: entry for entry in report["affected_missions"]}
        self.assertEqual(findings["M-1"]["finding"], "life_basis_changed")
        self.assertEqual(findings["M-2"]["finding"], "flown_over_limit")
        self.assertEqual(findings["M-2"]["over_limit_parts"], ["BAT-1"])
        self.assertEqual(findings["M-3"]["finding"], "newly_blocked")
        self.assertIn("PART_LIFE_EXCEEDED", findings["M-3"]["blockers"])

        scope = {(entry["part_serial"], entry["inspection"]) for entry in report["re_inspection_scope"]}
        self.assertIn(("BAT-1", "overhaul_assessment"), scope)
        self.assertIn(("BAT-1", "interval_inspection"), scope)
        self.assertIn(("PROP-1", "records_review"), scope)

        # 未来任务现在直接评估即被拦截
        decision = svc.evaluate_mission("M-3", role=DISPATCH)
        self.assertEqual(decision.status, "blocked")
        self.assertIn("PART_LIFE_EXCEEDED", blocker_codes(decision))

    def test_log_covered_by_manual_sync_is_not_double_counted(self):
        svc = make_service()
        ready_airframe(svc)
        create_mission(svc)
        svc.dispatch_mission("M-1", role=DISPATCH)
        svc.sync_part_life("BAT-1", {"used_cycles": 30, "used_hours": 9.0, "at": T_NEXT_DAY}, role=MAINT)
        report = svc.ingest_flight_log(
            {"id": "L-1", "mission_id": "M-1", "started_at": T_M1_START, "ended_at": T_M1_END,
             "cycles": 3, "hours": 0.5},
            role=MAINT,
        )
        self.assertTrue(report["late"])
        self.assertEqual(svc.store.parts["BAT-1"].used_cycles, 30)
        self.assertEqual([p["serial"] for p in report["skipped_parts"]], ["BAT-1"])

    def test_overlapping_logs_rejected(self):
        svc = make_service()
        ready_airframe(svc)
        create_mission(svc, "M-1")
        svc.dispatch_mission("M-1", role=DISPATCH)
        svc.ingest_flight_log(
            {"id": "L-1", "mission_id": "M-1", "started_at": T_M1_START, "ended_at": T_M1_END,
             "cycles": 1, "hours": 0.5},
            role=MAINT,
        )
        create_mission(svc, "M-2", start=T_M2_START, end=T_M2_END)
        svc.dispatch_mission("M-2", role=DISPATCH)
        with self.assertRaises(ConflictError):
            svc.ingest_flight_log(
                {"id": "L-2", "mission_id": "M-2", "started_at": "2026-09-21T11:15:00+08:00",
                 "ended_at": T_M2_END, "cycles": 1, "hours": 0.5},
                role=MAINT,
            )


class PersistenceTest(unittest.TestCase):
    def test_store_round_trip(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "store.json")
            svc = AirworthinessService(Store(path))
            ready_airframe(svc)
            create_mission(svc)
            svc.dispatch_mission("M-1", role=DISPATCH)
            svc.ingest_flight_log(
                {"id": "L-1", "mission_id": "M-1", "started_at": T_M1_START, "ended_at": T_M1_END,
                 "cycles": 2, "hours": 0.5},
                role=MAINT,
            )

            reloaded = AirworthinessService(Store(path))
            self.assertEqual(reloaded.store.parts["BAT-1"].used_cycles, 2)
            mission = reloaded.store.missions["M-1"]
            self.assertEqual(mission.status, "completed")
            self.assertEqual(len(reloaded.store.snapshots), 1)
            self.assertEqual(len(reloaded.store.install_events), 2)
            decision = reloaded.evaluate_mission("M-1", role=DISPATCH)
            self.assertIsNotNone(decision)


if __name__ == "__main__":
    unittest.main()
