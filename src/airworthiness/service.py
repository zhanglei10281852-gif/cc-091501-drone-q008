"""适航管理核心服务。

职责：
- 基于机体、可序列化部件、固件、维修工单、检查签名、任务载荷与环境限制，
  计算指定时刻的有效配置与放行状态，条件不满足时拦截任务；
- 部件拆装形成连续履历，飞行引用的配置快照不可变；
- 维修与任务通过占用记录互斥，同一机体同一时段只有一个有效配置；
- 紧急放行由独立角色批准，并带明确失效条件；
- 迟到飞行日志补计寿命后，圈出所有可能受影响的任务与复检范围。
"""

from __future__ import annotations

from .errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ReleaseBlocked,
    ValidationError,
)
from .models import (
    Airframe,
    Blocker,
    ConfigSnapshot,
    EmergencyRelease,
    Environment,
    FirmwareUpdate,
    FlightLog,
    InspectionSignature,
    InstallEvent,
    LifeAdjustment,
    Mission,
    Occupancy,
    Part,
    Payload,
    Qualification,
    ReleaseDecision,
    SnapshotPart,
    WorkOrder,
)
from .store import Store
from .timeutil import FAR_FUTURE, iso, now_utc
from .views import FULL_DETAIL_ROLES

#: 以循环计寿命的部件类型（其余部件只计飞行小时）。
CYCLE_DRIVEN_PART_TYPES = frozenset({"battery"})

#: 紧急放行只允许覆盖文书/资质类阻断；安全硬限制（寿命超限、环境、载荷、
#: 占用冲突、检查不通过等）不可覆盖。
OVERRIDABLE_BLOCKERS = frozenset({
    "PART_LIFE_UNSYNCED",
    "INSPECTION_MISSING",
    "INSPECTION_EXPIRED",
    "INSPECTION_SIGNATURE_INVALID",
})

_MAINTENANCE = frozenset({"maintenance"})
_DISPATCHER = frozenset({"dispatcher"})
_FLIGHT_LOG = frozenset({"maintenance"})
_EMERGENCY = frozenset({"airworthiness_engineer"})


def _req(body, key):
    value = body.get(key)
    if value is None:
        raise ValidationError(f"缺少必填字段 {key}")
    return value


def _req_str(body, key):
    value = _req(body, key)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"字段 {key} 必须为非空字符串")
    return value.strip()


def _opt_str(body, key):
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"字段 {key} 必须为非空字符串")
    return value.strip()


def _as_float(value, key):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"字段 {key} 必须为数值")
    return float(value)


def _req_float(body, key):
    return _as_float(_req(body, key), key)


def _opt_float(body, key):
    value = body.get(key)
    return None if value is None else _as_float(value, key)


def _as_int(value, key):
    if isinstance(value, bool):
        raise ValidationError(f"字段 {key} 必须为整数")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int):
        raise ValidationError(f"字段 {key} 必须为整数")
    return value


def _req_int(body, key):
    return _as_int(_req(body, key), key)


def _opt_int(body, key):
    value = body.get(key)
    return None if value is None else _as_int(value, key)


def _opt_bool(body, key, default=False):
    value = body.get(key, default)
    if not isinstance(value, bool):
        raise ValidationError(f"字段 {key} 必须为布尔值")
    return value


def _req_ts(body, key):
    from .timeutil import parse_ts

    return parse_ts(_req(body, key), key)


def _opt_ts(body, key):
    from .timeutil import parse_ts

    value = body.get(key)
    return parse_ts(value, key) if value is not None else None


def _req_str_list(body, key):
    value = _req(body, key)
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ValidationError(f"字段 {key} 必须为字符串数组")
    return [v.strip() for v in value]


class AirworthinessService:
    def __init__(self, store: Store):
        self.store = store

    # ---------- 通用 ----------

    @staticmethod
    def _require_role(role, allowed, action):
        if role not in allowed:
            raise ForbiddenError(f"当前角色无权执行：{action}")

    def _persist(self):
        self.store.save()

    def _get_airframe(self, airframe_id):
        try:
            return self.store.airframes[airframe_id]
        except KeyError:
            raise NotFoundError(f"机体 {airframe_id} 不存在") from None

    def _get_part(self, serial):
        try:
            return self.store.parts[serial]
        except KeyError:
            raise NotFoundError(f"部件 {serial} 不存在") from None

    def _get_mission(self, mission_id):
        try:
            return self.store.missions[mission_id]
        except KeyError:
            raise NotFoundError(f"任务 {mission_id} 不存在") from None

    # ---------- 登记 ----------

    def register_airframe(self, body, *, role):
        self._require_role(role, _MAINTENANCE, "登记机体")
        positions = _req(body, "positions")
        if not isinstance(positions, dict) or not positions or not all(
            isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip()
            for k, v in positions.items()
        ):
            raise ValidationError("positions 必须为 位置->部件类型 的非空字典")
        min_temp = _req_float(body, "min_temp_c")
        max_temp = _req_float(body, "max_temp_c")
        if min_temp >= max_temp:
            raise ValidationError("min_temp_c 必须小于 max_temp_c")
        airframe = Airframe(
            id=_req_str(body, "id"),
            model=_req_str(body, "model"),
            serial=_req_str(body, "serial"),
            positions={k.strip(): v.strip() for k, v in positions.items()},
            approved_firmware=_req_str_list(body, "approved_firmware"),
            required_inspections=_req_str_list(body, "required_inspections"),
            compatible_payloads=_req_str_list(body, "compatible_payloads"),
            max_payload_kg=_req_float(body, "max_payload_kg"),
            max_wind_mps=_req_float(body, "max_wind_mps"),
            min_temp_c=min_temp,
            max_temp_c=max_temp,
            allow_precipitation=_opt_bool(body, "allow_precipitation"),
        )
        if airframe.max_payload_kg < 0 or airframe.max_wind_mps < 0:
            raise ValidationError("载荷与风速限制不得为负")
        with self.store.atomic():
            if airframe.id in self.store.airframes:
                raise ConflictError(f"机体 {airframe.id} 已存在")
            self.store.airframes[airframe.id] = airframe
            self._persist()
        return airframe.to_dict()

    def register_part(self, body, *, role):
        self._require_role(role, _MAINTENANCE, "登记部件")
        part = Part(
            serial=_req_str(body, "serial"),
            type=_req_str(body, "type"),
            model=_req_str(body, "model"),
            cycle_limit=_opt_int(body, "cycle_limit"),
            hour_limit=_opt_float(body, "hour_limit"),
            inspection_interval_cycles=_opt_int(body, "inspection_interval_cycles"),
            used_cycles=_opt_int(body, "used_cycles") or 0,
            used_hours=_opt_float(body, "used_hours") or 0.0,
            life_synced_at=_opt_ts(body, "life_synced_at"),
        )
        if part.used_cycles < 0 or part.used_hours < 0:
            raise ValidationError("已用寿命不得为负")
        for limit in (part.cycle_limit, part.inspection_interval_cycles):
            if limit is not None and limit <= 0:
                raise ValidationError("寿命与检查间隔限制必须为正数")
        if part.hour_limit is not None and part.hour_limit <= 0:
            raise ValidationError("寿命与检查间隔限制必须为正数")
        with self.store.atomic():
            if part.serial in self.store.parts:
                raise ConflictError(f"部件 {part.serial} 已存在")
            self.store.parts[part.serial] = part
            self._persist()
        return part.to_dict()

    def grant_qualification(self, body, *, role):
        self._require_role(role, _MAINTENANCE, "登记人员资质")
        qualification = Qualification(
            personnel_id=_req_str(body, "personnel_id"),
            kind=_req_str(body, "kind"),
            valid_from=_req_ts(body, "valid_from"),
            valid_until=_req_ts(body, "valid_until"),
        )
        if qualification.valid_until <= qualification.valid_from:
            raise ValidationError("资质有效期必须晚于生效时间")
        with self.store.atomic():
            self.store.qualifications.append(qualification)
            self._persist()
        return qualification.to_dict()

    # ---------- 装配履历 ----------

    def install_part(self, airframe_id, body, *, role):
        self._require_role(role, _MAINTENANCE, "安装部件")
        part_serial = _req_str(body, "part_serial")
        position = _req_str(body, "position")
        at = _req_ts(body, "at")
        with self.store.atomic():
            airframe = self._get_airframe(airframe_id)
            part = self._get_part(part_serial)
            expected = airframe.positions.get(position)
            if expected is None:
                raise ValidationError(f"机体 {airframe_id} 不存在安装位置 {position}")
            if part.type != expected:
                raise ValidationError(f"位置 {position} 需要 {expected} 部件，{part_serial} 为 {part.type}")
            self._assert_no_mission_occupancy(airframe_id, at)
            for event in self.store.install_events:
                if event.airframe_id == airframe_id and event.position == position:
                    self._check_history_appendable(event, at, f"位置 {position}")
                    end = event.removed_at or FAR_FUTURE
                    if event.installed_at <= at < end:
                        raise ConflictError(f"位置 {position} 在该时刻已安装部件 {event.part_serial}，需先拆除")
                if event.part_serial == part_serial:
                    self._check_history_appendable(event, at, f"部件 {part_serial}")
                    end = event.removed_at or FAR_FUTURE
                    if event.installed_at <= at < end:
                        raise ConflictError(f"部件 {part_serial} 在该时刻仍安装在机体 {event.airframe_id}")
            event = InstallEvent(
                id=self.store.next_id("install", "ins"),
                airframe_id=airframe_id,
                part_serial=part_serial,
                part_type=part.type,
                position=position,
                installed_at=at,
            )
            self.store.install_events.append(event)
            self._persist()
            return event.to_dict()

    @staticmethod
    def _check_history_appendable(event, at, label):
        if at < event.installed_at:
            raise ValidationError(f"操作时间早于{label}既有履历，无法保持连续履历")

    def remove_part(self, airframe_id, body, *, role):
        self._require_role(role, _MAINTENANCE, "拆除部件")
        part_serial = _req_str(body, "part_serial")
        at = _req_ts(body, "at")
        with self.store.atomic():
            self._get_airframe(airframe_id)
            open_event = next(
                (
                    e
                    for e in self.store.install_events
                    if e.airframe_id == airframe_id and e.part_serial == part_serial and e.removed_at is None
                ),
                None,
            )
            if open_event is None:
                raise NotFoundError(f"部件 {part_serial} 未安装在机体 {airframe_id}")
            if at < open_event.installed_at:
                raise ValidationError("拆除时间早于安装时间")
            for event in self.store.install_events:
                if event is open_event:
                    continue
                if event.airframe_id == airframe_id and event.position == open_event.position:
                    self._check_history_appendable(event, at, f"位置 {open_event.position}")
                if event.part_serial == part_serial:
                    self._check_history_appendable(event, at, f"部件 {part_serial}")
            self._assert_no_mission_occupancy(airframe_id, at)
            open_event.removed_at = at
            self._persist()
            return open_event.to_dict()

    def activate_firmware(self, airframe_id, body, *, role):
        self._require_role(role, _MAINTENANCE, "更新固件")
        version = _req_str(body, "version")
        at = _req_ts(body, "at")
        with self.store.atomic():
            self._get_airframe(airframe_id)
            self._assert_no_mission_occupancy(airframe_id, at)
            history = [u for u in self.store.firmware_updates if u.airframe_id == airframe_id]
            if history and at < max(u.activated_at for u in history):
                raise ValidationError("固件生效时间早于既有记录")
            update = FirmwareUpdate(airframe_id=airframe_id, version=version, activated_at=at)
            self.store.firmware_updates.append(update)
            self._persist()
            return update.to_dict()

    def sync_part_life(self, serial, body, *, role):
        """人工同步部件寿命（如换新电池后录入循环数），差额计入台账。"""
        self._require_role(role, _MAINTENANCE, "同步部件寿命")
        used_cycles = _req_int(body, "used_cycles")
        used_hours = _req_float(body, "used_hours")
        at = _req_ts(body, "at")
        if used_cycles < 0 or used_hours < 0:
            raise ValidationError("寿命数值不得为负")
        with self.store.atomic():
            part = self._get_part(serial)
            delta_cycles = used_cycles - part.used_cycles
            delta_hours = used_hours - part.used_hours
            part.used_cycles = used_cycles
            part.used_hours = used_hours
            part.life_synced_at = at
            self.store.life_adjustments.append(
                LifeAdjustment(
                    id=self.store.next_id("adjustment", "adj"),
                    part_serial=serial,
                    at=at,
                    delta_cycles=delta_cycles,
                    delta_hours=delta_hours,
                    kind="sync",
                    ref_id=f"sync:{serial}:{at.isoformat()}",
                    note="人工寿命同步",
                )
            )
            self._persist()
            return {
                "serial": serial,
                "used_cycles": used_cycles,
                "used_hours": used_hours,
                "life_synced_at": iso(at),
                "delta_cycles": delta_cycles,
                "delta_hours": delta_hours,
            }

    # ---------- 检查签名 ----------

    def sign_inspection(self, airframe_id, body, *, role):
        self._require_role(role, _MAINTENANCE, "签署检查")
        kind = _req_str(body, "inspection_kind")
        personnel_id = _req_str(body, "personnel_id")
        signed_at = _req_ts(body, "signed_at")
        result = _req_str(body, "result")
        valid_until = _req_ts(body, "valid_until")
        if result not in ("pass", "fail"):
            raise ValidationError("检查结果必须为 pass 或 fail")
        if valid_until <= signed_at:
            raise ValidationError("签名有效期必须晚于签署时间")
        with self.store.atomic():
            self._get_airframe(airframe_id)
            signature = InspectionSignature(
                id=self.store.next_id("signature", "sig"),
                airframe_id=airframe_id,
                kind=kind,
                personnel_id=personnel_id,
                signed_at=signed_at,
                result=result,
                valid_until=valid_until,
            )
            self.store.signatures.append(signature)
            qualification_valid = self._qualification_covers(personnel_id, kind, signed_at)
            self._persist()
            payload = signature.to_dict()
            payload["qualification_valid"] = qualification_valid
            return payload

    # ---------- 工单与占用 ----------

    def open_work_order(self, body, *, role):
        self._require_role(role, _MAINTENANCE, "开立工单")
        order_id = _req_str(body, "id")
        airframe_id = _req_str(body, "airframe_id")
        order = WorkOrder(
            id=order_id,
            airframe_id=airframe_id,
            kind=_req_str(body, "kind"),
            grounding=_opt_bool(body, "grounding", default=True),
            opened_at=_req_ts(body, "opened_at"),
        )
        with self.store.atomic():
            self._get_airframe(airframe_id)
            if order_id in self.store.work_orders:
                raise ConflictError(f"工单 {order_id} 已存在")
            if order.grounding:
                self._acquire_occupancy(airframe_id, "maintenance", order_id, order.opened_at, None)
            self.store.work_orders[order_id] = order
            self._persist()
            return order.to_dict()

    def close_work_order(self, work_order_id, body, *, role):
        self._require_role(role, _MAINTENANCE, "关闭工单")
        closed_at = _req_ts(body, "closed_at")
        with self.store.atomic():
            order = self.store.work_orders.get(work_order_id)
            if order is None:
                raise NotFoundError(f"工单 {work_order_id} 不存在")
            if order.closed_at is not None:
                raise ConflictError(f"工单 {work_order_id} 已关闭")
            if closed_at < order.opened_at:
                raise ValidationError("关闭时间早于开立时间")
            order.closed_at = closed_at
            for occupancy in self.store.occupancies:
                if occupancy.kind == "maintenance" and occupancy.ref_id == order.id and occupancy.end is None:
                    occupancy.end = closed_at
            self._persist()
            return order.to_dict()

    # ---------- 任务 ----------

    def create_mission(self, body, *, role):
        self._require_role(role, _DISPATCHER, "创建任务")
        payload_body = _req(body, "payload")
        environment_body = _req(body, "environment")
        if not isinstance(payload_body, dict) or not isinstance(environment_body, dict):
            raise ValidationError("payload 与 environment 必须为对象")
        mission = Mission(
            id=_req_str(body, "id"),
            airframe_id=_req_str(body, "airframe_id"),
            payload=Payload(
                kind=_req_str(payload_body, "kind"),
                weight_kg=_req_float(payload_body, "weight_kg"),
            ),
            planned_start=_req_ts(body, "planned_start"),
            planned_end=_req_ts(body, "planned_end"),
            environment=Environment(
                wind_mps=_req_float(environment_body, "wind_mps"),
                temp_c=_req_float(environment_body, "temp_c"),
                precipitation=_opt_bool(environment_body, "precipitation"),
            ),
        )
        if mission.planned_end <= mission.planned_start:
            raise ValidationError("任务结束时间必须晚于开始时间")
        if mission.payload.weight_kg < 0 or mission.environment.wind_mps < 0:
            raise ValidationError("载荷重量与风速不得为负")
        with self.store.atomic():
            self._get_airframe(mission.airframe_id)
            if mission.id in self.store.missions:
                raise ConflictError(f"任务 {mission.id} 已存在")
            self.store.missions[mission.id] = mission
            self._persist()
            return mission.to_dict()

    def evaluate_mission(self, mission_id, at=None, *, role):
        with self.store.atomic():
            mission = self._get_mission(mission_id)
            airframe = self._get_airframe(mission.airframe_id)
            return self._evaluate(airframe, mission, at or mission.planned_start)

    def dispatch_mission(self, mission_id, at=None, *, role):
        """放行任务：评估通过才占用机体并固化配置快照，否则拦截。"""
        self._require_role(role, _DISPATCHER, "放行任务")
        with self.store.atomic():
            mission = self._get_mission(mission_id)
            airframe = self._get_airframe(mission.airframe_id)
            if mission.status != "scheduled":
                raise ConflictError(f"任务 {mission_id} 当前状态为 {mission.status}，不可放行")
            at = at or mission.planned_start
            decision = self._evaluate(airframe, mission, at)
            if decision.status != "released":
                raise ReleaseBlocked(decision)
            self._acquire_occupancy(airframe.id, "mission", mission.id, mission.planned_start, mission.planned_end)
            snapshot = self._create_snapshot(airframe, at)
            mission.status = "dispatched"
            mission.config_id = snapshot.id
            mission.dispatched_at = at
            decision.config_id = snapshot.id
            self._persist()
            return decision

    # ---------- 飞行日志与寿命补计 ----------

    def ingest_flight_log(self, body, *, role):
        self._require_role(role, _FLIGHT_LOG, "记录飞行日志")
        log_id = _req_str(body, "id")
        mission_id = _req_str(body, "mission_id")
        started_at = _req_ts(body, "started_at")
        ended_at = _req_ts(body, "ended_at")
        cycles = _req_int(body, "cycles")
        hours = _req_float(body, "hours")
        if ended_at <= started_at:
            raise ValidationError("飞行结束时间必须晚于开始时间")
        if cycles < 0 or hours < 0:
            raise ValidationError("循环数与飞行小时不得为负")
        with self.store.atomic():
            if log_id in self.store.flight_logs:
                raise ConflictError(f"飞行日志 {log_id} 已存在")
            mission = self._get_mission(mission_id)
            if mission.config_id is None:
                raise ValidationError(f"任务 {mission_id} 尚未放行，无法记录飞行日志")
            if any(log.mission_id == mission_id for log in self.store.flight_logs.values()):
                raise ConflictError(f"任务 {mission_id} 已有飞行日志")
            for other in self.store.flight_logs.values():
                if (
                    other.airframe_id == mission.airframe_id
                    and started_at < other.ended_at
                    and other.started_at < ended_at
                ):
                    raise ConflictError(f"飞行日志与 {other.id} 时段重叠，疑似重复计数")
            snapshot = self.store.snapshots[mission.config_id]
            late = any(
                log.airframe_id == mission.airframe_id and log.started_at > started_at
                for log in self.store.flight_logs.values()
            )
            if not late:
                late = any(
                    (part.life_synced_at is not None and part.life_synced_at > started_at)
                    for part in (self.store.parts.get(sp.serial) for sp in snapshot.parts)
                    if part is not None
                )
            log = FlightLog(
                id=log_id,
                mission_id=mission_id,
                airframe_id=mission.airframe_id,
                config_id=mission.config_id,
                started_at=started_at,
                ended_at=ended_at,
                cycles=cycles,
                hours=hours,
                received_at=now_utc(),
            )
            self.store.flight_logs[log.id] = log
            applied, skipped = [], []
            for snapshot_part in snapshot.parts:
                part = self.store.parts[snapshot_part.serial]
                if part.life_synced_at is not None and ended_at <= part.life_synced_at:
                    skipped.append(
                        {"serial": part.serial, "reason": "该部件寿命已被人工同步覆盖，跳过以避免重复计数"}
                    )
                    continue
                delta_cycles = cycles if part.type in CYCLE_DRIVEN_PART_TYPES else 0
                before_cycles, before_hours = part.used_cycles, part.used_hours
                part.used_cycles += delta_cycles
                part.used_hours += hours
                self.store.life_adjustments.append(
                    LifeAdjustment(
                        id=self.store.next_id("adjustment", "adj"),
                        part_serial=part.serial,
                        at=started_at,
                        delta_cycles=delta_cycles,
                        delta_hours=hours,
                        kind="flight",
                        ref_id=log.id,
                    )
                )
                applied.append(
                    {
                        "serial": part.serial,
                        "type": part.type,
                        "delta_cycles": delta_cycles,
                        "delta_hours": hours,
                        "before_cycles": before_cycles,
                        "after_cycles": part.used_cycles,
                        "before_hours": before_hours,
                        "after_hours": part.used_hours,
                    }
                )
            if mission.status == "dispatched":
                mission.status = "completed"
            report = self._build_impact_report(log, snapshot, applied, skipped, late)
            self._persist()
            return report

    # ---------- 紧急放行 ----------

    def approve_emergency_release(self, body, *, role):
        self._require_role(role, _EMERGENCY, "批准紧急放行")
        release_id = _req_str(body, "id")
        airframe_id = _req_str(body, "airframe_id")
        mission_id = _opt_str(body, "mission_id")
        requested_by = _req_str(body, "requested_by")
        approved_by = _req_str(body, "approved_by")
        approved_at = _req_ts(body, "approved_at")
        expires_at = _req_ts(body, "expires_at")
        overrides = _req_str_list(body, "overrides")
        max_additional_cycles = _opt_int(body, "max_additional_cycles")
        conditions = _req_str(body, "conditions")
        if approved_by == requested_by:
            raise ForbiddenError("批准人不得与申请人相同（独立性要求）")
        if expires_at <= approved_at:
            raise ValidationError("失效时间必须晚于批准时间")
        not_overridable = sorted(set(overrides) - OVERRIDABLE_BLOCKERS)
        if not_overridable:
            raise ValidationError(f"以下阻断项不允许紧急放行覆盖: {not_overridable}")
        if max_additional_cycles is not None and max_additional_cycles <= 0:
            raise ValidationError("附加循环上限必须为正数")
        with self.store.atomic():
            self._get_airframe(airframe_id)
            if mission_id is not None:
                mission = self._get_mission(mission_id)
                if mission.airframe_id != airframe_id:
                    raise ValidationError("紧急放行绑定的任务不属于该机体")
            if release_id in self.store.emergency_releases:
                raise ConflictError(f"紧急放行 {release_id} 已存在")
            invalid_signers = {
                s.personnel_id
                for s in self.store.signatures
                if s.airframe_id == airframe_id
                and not self._qualification_covers(s.personnel_id, s.kind, s.signed_at)
            }
            if approved_by in invalid_signers:
                raise ForbiddenError("批准人为涉事检查签字人，违反独立性要求")
            release = EmergencyRelease(
                id=release_id,
                airframe_id=airframe_id,
                mission_id=mission_id,
                requested_by=requested_by,
                approved_by=approved_by,
                approved_at=approved_at,
                expires_at=expires_at,
                overrides=overrides,
                max_additional_cycles=max_additional_cycles,
                conditions=conditions,
            )
            self.store.emergency_releases[release.id] = release
            self._persist()
            return release.to_dict()

    # ---------- 查询 ----------

    def effective_config_view(self, airframe_id, at, *, role):
        self._require_role(role, FULL_DETAIL_ROLES, "查看有效配置")
        with self.store.atomic():
            config = self.effective_config_at(airframe_id, at)
            parts = [
                {
                    "position": position,
                    "serial": part.serial,
                    "type": part.type,
                    "used_cycles": part.used_cycles,
                    "cycle_limit": part.cycle_limit,
                    "used_hours": part.used_hours,
                    "hour_limit": part.hour_limit,
                    "life_synced_at": iso(part.life_synced_at),
                }
                for position, part in sorted(config["parts"].items())
            ]
            return {
                "airframe_id": airframe_id,
                "at": iso(at),
                "firmware_version": config["firmware_version"],
                "parts": parts,
            }

    def part_history(self, airframe_id, *, role):
        self._require_role(role, FULL_DETAIL_ROLES, "查看部件履历")
        with self.store.atomic():
            self._get_airframe(airframe_id)
            events = sorted(
                (e for e in self.store.install_events if e.airframe_id == airframe_id),
                key=lambda e: (e.installed_at, e.id),
            )
            return {
                "airframe_id": airframe_id,
                "events": [
                    {
                        "position": e.position,
                        "part_serial": e.part_serial,
                        "part_type": e.part_type,
                        "installed_at": iso(e.installed_at),
                        "removed_at": iso(e.removed_at),
                    }
                    for e in events
                ],
            }

    # ---------- 内部：配置与占用 ----------

    def effective_config_at(self, airframe_id, at):
        """指定时刻的有效配置：由履历事件推导，结果唯一。"""
        airframe = self._get_airframe(airframe_id)
        installed = {}
        for event in self.store.install_events:
            if (
                event.airframe_id == airframe.id
                and event.installed_at <= at
                and (event.removed_at is None or event.removed_at > at)
            ):
                installed[event.position] = self.store.parts[event.part_serial]
        firmware = None
        for update in self.store.firmware_updates:
            if update.airframe_id == airframe.id and update.activated_at <= at:
                if firmware is None or update.activated_at > firmware.activated_at:
                    firmware = update
        return {
            "airframe_id": airframe.id,
            "at": at,
            "parts": installed,
            "firmware_version": firmware.version if firmware else None,
        }

    def _occupancy_conflicts(self, airframe_id, start, end, ignore_ref=None):
        return [
            occupancy
            for occupancy in self.store.occupancies
            if occupancy.airframe_id == airframe_id
            and occupancy.ref_id != ignore_ref
            and occupancy.overlaps(start, end)
        ]

    def _acquire_occupancy(self, airframe_id, kind, ref_id, start, end):
        conflicts = self._occupancy_conflicts(airframe_id, start, end)
        if conflicts:
            holder = conflicts[0]
            raise ConflictError(
                f"机体 {airframe_id} 在该时段已被{holder.kind}占用",
                detail={"kind": holder.kind, "ref_id": holder.ref_id},
            )
        occupancy = Occupancy(
            id=self.store.next_id("occupancy", "occ"),
            airframe_id=airframe_id,
            kind=kind,
            ref_id=ref_id,
            start=start,
            end=end,
        )
        self.store.occupancies.append(occupancy)
        return occupancy

    def _assert_no_mission_occupancy(self, airframe_id, at):
        for occupancy in self.store.occupancies:
            if (
                occupancy.airframe_id == airframe_id
                and occupancy.kind == "mission"
                and occupancy.overlaps(at, FAR_FUTURE)
            ):
                raise ConflictError(
                    f"机体 {airframe_id} 存在任务占用 {occupancy.ref_id}，禁止变更装配",
                    detail={"ref_id": occupancy.ref_id},
                )

    def _create_snapshot(self, airframe, at):
        config = self.effective_config_at(airframe.id, at)
        snapshot = ConfigSnapshot(
            id=self.store.next_id("snapshot", "cfg"),
            airframe_id=airframe.id,
            as_of=at,
            created_at=now_utc(),
            firmware_version=config["firmware_version"],
            parts=[
                SnapshotPart(
                    serial=part.serial,
                    type=part.type,
                    position=position,
                    used_cycles=part.used_cycles,
                    used_hours=part.used_hours,
                    cycle_limit=part.cycle_limit,
                    hour_limit=part.hour_limit,
                )
                for position, part in sorted(config["parts"].items())
            ],
        )
        self.store.snapshots[snapshot.id] = snapshot
        return snapshot

    # ---------- 内部：放行评估 ----------

    def _evaluate(self, airframe, mission, at):
        blockers: list[Blocker] = []
        life_basis: list[dict] = []
        signature_basis: list[dict] = []
        config = self.effective_config_at(airframe.id, at)
        installed = config["parts"]

        for position, expected in sorted(airframe.positions.items()):
            part = installed.get(position)
            if part is None:
                blockers.append(
                    Blocker("POSITION_EMPTY", f"位置 {position} 未安装 {expected} 部件",
                            {"position": position, "required_type": expected})
                )
            elif part.type != expected:
                blockers.append(
                    Blocker("PART_TYPE_MISMATCH", f"位置 {position} 部件类型不符",
                            {"position": position, "required_type": expected,
                             "actual_type": part.type, "serial": part.serial})
                )

        for position, part in sorted(installed.items()):
            life_basis.append(
                {
                    "position": position,
                    "serial": part.serial,
                    "type": part.type,
                    "used_cycles": part.used_cycles,
                    "cycle_limit": part.cycle_limit,
                    "used_hours": part.used_hours,
                    "hour_limit": part.hour_limit,
                    "life_synced": part.life_synced_at is not None,
                    "life_synced_at": iso(part.life_synced_at),
                }
            )
            if part.life_synced_at is None:
                blockers.append(
                    Blocker("PART_LIFE_UNSYNCED", f"部件 {part.serial} 循环寿命未同步",
                            {"position": position, "serial": part.serial})
                )
            if part.cycle_limit is not None and part.used_cycles >= part.cycle_limit:
                blockers.append(
                    Blocker("PART_LIFE_EXCEEDED", f"部件 {part.serial} 循环寿命已达上限",
                            {"position": position, "serial": part.serial,
                             "used_cycles": part.used_cycles, "cycle_limit": part.cycle_limit})
                )
            if part.hour_limit is not None and part.used_hours >= part.hour_limit:
                blockers.append(
                    Blocker("PART_LIFE_EXCEEDED", f"部件 {part.serial} 飞行小时已达上限",
                            {"position": position, "serial": part.serial,
                             "used_hours": part.used_hours, "hour_limit": part.hour_limit})
                )

        firmware = config["firmware_version"]
        if firmware is None:
            blockers.append(Blocker("FIRMWARE_MISSING", "机体无生效固件", {}))
        elif firmware not in airframe.approved_firmware:
            blockers.append(
                Blocker("FIRMWARE_NOT_APPROVED", f"固件 {firmware} 未在批准清单内", {"version": firmware})
            )

        for kind in airframe.required_inspections:
            basis = self._inspection_basis(airframe.id, kind, at)
            signature_basis.append(basis)
            if basis["status"] != "valid":
                code = {
                    "missing": "INSPECTION_MISSING",
                    "expired": "INSPECTION_EXPIRED",
                    "failed": "INSPECTION_FAILED",
                    "signature_invalid": "INSPECTION_SIGNATURE_INVALID",
                }[basis["status"]]
                blockers.append(Blocker(code, basis["message"], dict(basis)))

        for order in self.store.work_orders.values():
            if order.airframe_id == airframe.id and order.grounding and order.is_open_at(at):
                blockers.append(
                    Blocker("WORK_ORDER_OPEN", f"存在未关闭的停场工单 {order.id}",
                            {"work_order_id": order.id, "kind": order.kind})
                )

        if mission is not None:
            for occupancy in self._occupancy_conflicts(
                airframe.id, mission.planned_start, mission.planned_end, ignore_ref=mission.id
            ):
                blockers.append(
                    Blocker("OCCUPANCY_CONFLICT", "机体在任务时段内存在其它占用",
                            {"kind": occupancy.kind, "ref_id": occupancy.ref_id})
                )
            if mission.payload.kind not in airframe.compatible_payloads:
                blockers.append(
                    Blocker("PAYLOAD_INCOMPATIBLE", f"载荷 {mission.payload.kind} 与机型不兼容",
                            {"payload_kind": mission.payload.kind})
                )
            if mission.payload.weight_kg > airframe.max_payload_kg:
                blockers.append(
                    Blocker("PAYLOAD_OVERWEIGHT", "载荷重量超出机体限制",
                            {"weight_kg": mission.payload.weight_kg,
                             "max_payload_kg": airframe.max_payload_kg})
                )
            environment = mission.environment
            if environment.wind_mps > airframe.max_wind_mps:
                blockers.append(
                    Blocker("ENV_WIND_EXCEEDED", "风速超出机体限制",
                            {"wind_mps": environment.wind_mps, "max_wind_mps": airframe.max_wind_mps})
                )
            if not airframe.min_temp_c <= environment.temp_c <= airframe.max_temp_c:
                blockers.append(
                    Blocker("ENV_TEMP_OUT_OF_RANGE", "温度超出机体限制",
                            {"temp_c": environment.temp_c, "min_temp_c": airframe.min_temp_c,
                             "max_temp_c": airframe.max_temp_c})
                )
            if environment.precipitation and not airframe.allow_precipitation:
                blockers.append(Blocker("ENV_PRECIPITATION", "机体不允许在降水条件下运行", {}))

        status = "released" if not blockers else "blocked"
        via = "standard"
        emergency = None
        if blockers:
            matched, reason = self._match_emergency(airframe.id, mission, at, blockers)
            if matched is not None:
                status, via = "released", "emergency"
                emergency = {
                    "id": matched.id,
                    "approved_by": matched.approved_by,
                    "approved_at": iso(matched.approved_at),
                    "expires_at": iso(matched.expires_at),
                    "max_additional_cycles": matched.max_additional_cycles,
                    "conditions": matched.conditions,
                    "overrides": list(matched.overrides),
                }
            elif reason:
                emergency = {"valid": False, "reason": reason}
        return ReleaseDecision(
            airframe_id=airframe.id,
            mission_id=mission.id if mission else None,
            evaluated_at=at,
            status=status,
            via=via,
            blockers=blockers,
            life_basis=life_basis,
            signature_basis=signature_basis,
            emergency=emergency,
        )

    def _inspection_basis(self, airframe_id, kind, at):
        signatures = [
            s
            for s in self.store.signatures
            if s.airframe_id == airframe_id and s.kind == kind and s.signed_at <= at
        ]
        if not signatures:
            return {"kind": kind, "status": "missing", "message": f"缺少 {kind} 检查的有效签名"}
        signatures.sort(key=lambda s: (s.signed_at, s.id), reverse=True)
        latest = signatures[0]
        if latest.result == "fail":
            return {
                "kind": kind,
                "status": "failed",
                "message": f"{kind} 最近一次检查结果为不通过",
                "personnel_id": latest.personnel_id,
                "signed_at": iso(latest.signed_at),
                "valid_until": iso(latest.valid_until),
            }
        for signature in signatures:
            if signature.result != "pass" or signature.valid_until < at:
                continue
            if not self._qualification_covers(signature.personnel_id, kind, signature.signed_at):
                continue
            return {
                "kind": kind,
                "status": "valid",
                "message": "签名有效",
                "personnel_id": signature.personnel_id,
                "signed_at": iso(signature.signed_at),
                "valid_until": iso(signature.valid_until),
                "qualification_valid": True,
            }
        if latest.valid_until < at:
            status, message = "expired", f"{kind} 检查签名已超过有效期"
        else:
            status, message = "signature_invalid", f"{kind} 检查签字人资质在签署时已失效"
        return {
            "kind": kind,
            "status": status,
            "message": message,
            "personnel_id": latest.personnel_id,
            "signed_at": iso(latest.signed_at),
            "valid_until": iso(latest.valid_until),
            "qualification_valid": False,
        }

    def _qualification_covers(self, personnel_id, kind, at):
        return any(
            q.personnel_id == personnel_id and q.kind == kind and q.covers(at)
            for q in self.store.qualifications
        )

    def _match_emergency(self, airframe_id, mission, at, blockers):
        codes = {b.code for b in blockers}
        if not codes <= OVERRIDABLE_BLOCKERS:
            return None, "存在不可通过紧急放行覆盖的阻断项"
        reason = "无适用的紧急放行"
        for release in self.store.emergency_releases.values():
            if release.airframe_id != airframe_id or release.revoked:
                continue
            if release.mission_id is not None and (mission is None or release.mission_id != mission.id):
                continue
            if not codes <= set(release.overrides):
                reason = f"紧急放行 {release.id} 未覆盖全部阻断项"
                continue
            if at >= release.expires_at:
                reason = f"紧急放行 {release.id} 已过失效时间"
                continue
            if release.max_additional_cycles is not None:
                consumed = sum(
                    log.cycles
                    for log in self.store.flight_logs.values()
                    if log.airframe_id == airframe_id and log.started_at >= release.approved_at
                )
                if consumed >= release.max_additional_cycles:
                    reason = f"紧急放行 {release.id} 的附加循环已用尽"
                    continue
            return release, None
        return None, reason

    # ---------- 内部：迟到日志影响分析 ----------

    def _life_at(self, serial, at):
        """按当前台账回算部件在 at 时刻的寿命（含迟到补计）。"""
        part = self.store.parts[serial]
        cycles, hours = part.used_cycles, part.used_hours
        for adjustment in self.store.life_adjustments:
            if adjustment.part_serial == serial and adjustment.at > at:
                cycles -= adjustment.delta_cycles
                hours -= adjustment.delta_hours
        return cycles, hours

    def _build_impact_report(self, log, snapshot, applied, skipped, late):
        affected_serials = {entry["serial"] for entry in applied}
        window_start = log.started_at
        affected_missions = []
        for mission in self.store.missions.values():
            if mission.id == log.mission_id:
                continue
            mission_log = None
            if mission.config_id is not None:
                mission_snapshot = self.store.snapshots[mission.config_id]
                related = bool({p.serial for p in mission_snapshot.parts} & affected_serials)
                if related and mission.status == "completed":
                    mission_log = next(
                        (l for l in self.store.flight_logs.values() if l.mission_id == mission.id), None
                    )
            elif mission.status == "scheduled":
                config = self.effective_config_at(mission.airframe_id, mission.planned_start)
                related = bool({p.serial for p in config["parts"].values()} & affected_serials)
            else:
                related = False
            if not related:
                continue
            reference_time = mission_log.started_at if mission_log else mission.planned_start
            if reference_time < window_start:
                continue
            entry = {
                "mission_id": mission.id,
                "airframe_id": mission.airframe_id,
                "status": mission.status,
                "planned_start": iso(mission.planned_start),
            }
            if mission.status == "completed" and mission_log is not None:
                over_limit = []
                mission_snapshot = self.store.snapshots[mission.config_id]
                for snapshot_part in mission_snapshot.parts:
                    if snapshot_part.serial not in affected_serials:
                        continue
                    part = self.store.parts[snapshot_part.serial]
                    cycles, hours = self._life_at(snapshot_part.serial, mission_log.started_at)
                    if (part.cycle_limit is not None and cycles >= part.cycle_limit) or (
                        part.hour_limit is not None and hours >= part.hour_limit
                    ):
                        over_limit.append(snapshot_part.serial)
                entry["finding"] = "flown_over_limit" if over_limit else "life_basis_changed"
                if over_limit:
                    entry["over_limit_parts"] = over_limit
            elif mission.status in ("scheduled", "dispatched"):
                airframe = self._get_airframe(mission.airframe_id)
                decision = self._evaluate(airframe, mission, mission.planned_start)
                entry["finding"] = "newly_blocked" if decision.status == "blocked" else "still_released"
                if decision.status == "blocked":
                    entry["blockers"] = [b.code for b in decision.blockers]
            affected_missions.append(entry)

        re_inspection_scope = []
        for serial in sorted(affected_serials):
            part = self.store.parts[serial]
            entries = []
            if (part.cycle_limit is not None and part.used_cycles >= part.cycle_limit) or (
                part.hour_limit is not None and part.used_hours >= part.hour_limit
            ):
                entries.append(("overhaul_assessment", "life_limit_exceeded"))
            applied_entry = next((a for a in applied if a["serial"] == serial), None)
            if applied_entry and part.inspection_interval_cycles:
                interval = part.inspection_interval_cycles
                if applied_entry["after_cycles"] // interval > applied_entry["before_cycles"] // interval:
                    entries.append(("interval_inspection", "inspection_interval_crossed"))
            if not entries:
                entries.append(("records_review", "life_basis_changed"))
            for inspection, reason in entries:
                re_inspection_scope.append(
                    {
                        "part_serial": serial,
                        "part_type": part.type,
                        "inspection": inspection,
                        "reason": reason,
                    }
                )

        return {
            "log_id": log.id,
            "mission_id": log.mission_id,
            "airframe_id": log.airframe_id,
            "late": late,
            "window_start": iso(window_start),
            "life_applied": applied,
            "skipped_parts": skipped,
            "affected_missions": affected_missions,
            "re_inspection_scope": re_inspection_scope,
        }
