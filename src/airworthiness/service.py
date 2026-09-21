"""适航领域服务：有效配置、寿命、放行、紧急放行、迟到日志追溯。

所有方法均线程安全；评估类读操作也在存储锁内完成，保证“同一时刻同一机体
至多一个有效配置”，且维修/任务并发不会产生两套放行结论。
"""

from typing import Optional

from . import timeutil
from .errors import AuthorizationError, ConflictError, NotFoundError, ValidationError
from .models import (
    KIND_FLIGHT_CONTROLLER,
    OVERRIDABLE_BLOCKERS,
    ROLE_MECHANIC,
    ROLE_READONLY,
    ROLE_REGULATOR,
    ROLE_RELEASE,
    ROLES,
    Aircraft,
    Certification,
    Component,
    EmergencyRelease,
    FlightLog,
    Inspection,
    InstallRecord,
    Mission,
    Person,
    ReleaseEvaluation,
    WorkOrder,
)

# 日志晚于实际落地时间超过该宽限期即视为“迟到”
DEFAULT_LATE_GRACE_SECONDS = 3600


def _iso(dt) -> str:
    return timeutil.format_value(dt)


class AirworthinessService:
    def __init__(self, store, late_grace_seconds: int = DEFAULT_LATE_GRACE_SECONDS, clock=None):
        self.store = store
        self.late_grace_seconds = late_grace_seconds
        self._clock = clock or timeutil.now

    def _now(self):
        return self._clock()

    # ================================================================ 辅助
    def _person(self, person_id: str) -> Person:
        row = self.store.people.get(person_id)
        if row is None:
            raise NotFoundError(f"人员不存在: {person_id}")
        return self._hydrate_person(row)

    def _hydrate_person(self, row: dict) -> Person:
        certs = [
            Certification(
                id=c["id"],
                person_id=row["id"],
                scope=c["scope"],
                valid_from=timeutil.parse(c["validFrom"]),
                valid_until=timeutil.parse(c["validUntil"]),
            )
            for c in row.get("certifications", [])
        ]
        return Person(id=row["id"], name=row["name"], role=row["role"], certifications=certs)

    def _aircraft(self, aircraft_id: str) -> Aircraft:
        row = self.store.aircraft.get(aircraft_id)
        if row is None:
            raise NotFoundError(f"机体不存在: {aircraft_id}")
        return Aircraft(
            id=row["id"],
            model=row["model"],
            required_positions=row["requiredPositions"],
            airframe_cycle_limit=row.get("airframeCycleLimit"),
            airframe_hour_limit=row.get("airframeHourLimit"),
            max_wind_kt=row.get("maxWindKt"),
            min_temp_c=row.get("minTempC"),
            max_temp_c=row.get("maxTempC"),
            allow_precipitation=row.get("allowPrecipitation", True),
            max_payload_kg=row.get("maxPayloadKg"),
            inspection_program=row.get("inspectionProgram", {}),
        )

    def _component(self, serial: str) -> Component:
        row = self.store.components.get(serial)
        if row is None:
            raise NotFoundError(f"部件不存在: {serial}")
        return Component(
            serial=row["serial"],
            kind=row["kind"],
            model=row["model"],
            cycle_limit=row.get("cycleLimit"),
            hour_limit=row.get("hourLimit"),
            inspection_every_cycles=row.get("inspectionEveryCycles"),
            inspection_every_hours=row.get("inspectionEveryHours"),
        )

    def _installs(self, aircraft_id: str) -> list[InstallRecord]:
        return [
            InstallRecord(
                id=r["id"],
                aircraft_id=r["aircraftId"],
                position=r["position"],
                component_serial=r["componentSerial"],
                installed_at=timeutil.parse(r["installedAt"]),
                removed_at=timeutil.parse(r["removedAt"]) if r.get("removedAt") else None,
                baseline_cycles=r.get("baselineCycles"),
                baseline_hours=r.get("baselineHours"),
                removed_by=r.get("removedBy"),
            )
            for r in self.store.installs
            if r["aircraftId"] == aircraft_id
        ]

    def _flights(self, aircraft_id: str) -> list[FlightLog]:
        out = []
        for r in self.store.flight_logs:
            if r["aircraftId"] != aircraft_id:
                continue
            out.append(
                FlightLog(
                    id=r["id"],
                    aircraft_id=aircraft_id,
                    off_block_at=timeutil.parse(r["offBlockAt"]),
                    on_block_at=timeutil.parse(r["onBlockAt"]),
                    cycles=float(r["cycles"]),
                    hours=float(r["hours"]),
                    received_at=timeutil.parse(r["receivedAt"]),
                    mission_id=r.get("missionId"),
                    payload=r.get("payload", []),
                    weather=r.get("weather", {}),
                    config_snapshot=r.get("configSnapshot"),
                    late=bool(r.get("late")),
                )
            )
        return out

    def _mission(self, mission_id: str) -> Mission:
        row = self.store.missions.get(mission_id)
        if row is None:
            raise NotFoundError(f"任务不存在: {mission_id}")
        return Mission(
            id=row["id"],
            aircraft_id=row["aircraftId"],
            planned_start=timeutil.parse(row["plannedStart"]),
            planned_end=timeutil.parse(row["plannedEnd"]),
            payload=row.get("payload", []),
            forecast=row.get("forecast", {}),
            status=row.get("status", "planned"),
            created_by=row.get("createdBy"),
        )

    def _inspections(self, aircraft_id: str) -> list[Inspection]:
        out = []
        for r in self.store.inspections:
            if r["aircraftId"] != aircraft_id:
                continue
            out.append(
                Inspection(
                    id=r["id"],
                    aircraft_id=aircraft_id,
                    scope=r["scope"],
                    signed_by=r["signedBy"],
                    signed_at=timeutil.parse(r["signedAt"]),
                    valid_from=timeutil.parse(r["validFrom"]),
                    valid_until=timeutil.parse(r["validUntil"]),
                    component_serial=r.get("componentSerial"),
                    result=r.get("result", "pass"),
                )
            )
        return out

    def _work_orders(self, aircraft_id: str) -> list[WorkOrder]:
        out = []
        for r in self.store.work_orders:
            if r["aircraftId"] != aircraft_id:
                continue
            out.append(
                WorkOrder(
                    id=r["id"],
                    aircraft_id=aircraft_id,
                    title=r["title"],
                    opened_at=timeutil.parse(r["openedAt"]),
                    closed_at=timeutil.parse(r["closedAt"]) if r.get("closedAt") else None,
                    created_by=r.get("createdBy"),
                )
            )
        return out

    def _require_role(self, actor: Person, roles: set[str]) -> None:
        if actor.role not in roles:
            raise AuthorizationError(f"角色 {actor.role} 无权执行该操作，需要: {sorted(roles)}")

    def authorize(self, actor_id: str, roles: set[str]) -> Person:
        """公开的岗位鉴权入口（供适配层复用同套岗位约束）。"""
        with self.store.lock:
            actor = self._person(actor_id)
            self._require_role(actor, roles)
            return actor

    # ================================================================ 登记
    def register_person(self, data: dict) -> dict:
        with self.store.lock:
            pid = data["id"]
            if pid in self.store.people:
                raise ConflictError(f"人员已存在: {pid}")
            role = data.get("role")
            if role not in ROLES:
                raise ValidationError(f"未知角色: {role!r}，允许: {ROLES}")
            row = {"id": pid, "name": data["name"], "role": role, "certifications": []}
            self.store.people[pid] = row
            self.store.save()
            return {"id": pid, "name": row["name"], "role": role}

    def add_certification(self, person_id: str, data: dict, actor_id: str) -> dict:
        with self.store.lock:
            actor = self._person(actor_id)
            self._require_role(actor, {ROLE_REGULATOR})
            person = self._person(person_id)
            valid_from = timeutil.parse(data["validFrom"])
            valid_until = timeutil.parse(data["validUntil"])
            if valid_until <= valid_from:
                raise ValidationError("资质失效时间必须晚于生效时间")
            cid = self.store.next_id("cert")
            row = {
                "id": cid,
                "scope": data["scope"],
                "validFrom": _iso(valid_from),
                "validUntil": _iso(valid_until),
            }
            self.store.people[person_id]["certifications"].append(row)
            self.store.save()
            return row | {"personId": person_id}

    def register_aircraft(self, data: dict) -> dict:
        with self.store.lock:
            aid = data["id"]
            if aid in self.store.aircraft:
                raise ConflictError(f"机体已存在: {aid}")
            if not data.get("requiredPositions"):
                raise ValidationError("机体必须声明必需装配位")
            row = {
                "id": aid,
                "model": data["model"],
                "requiredPositions": dict(data["requiredPositions"]),
                "airframeCycleLimit": data.get("airframeCycleLimit"),
                "airframeHourLimit": data.get("airframeHourLimit"),
                "maxWindKt": data.get("maxWindKt"),
                "minTempC": data.get("minTempC"),
                "maxTempC": data.get("maxTempC"),
                "allowPrecipitation": data.get("allowPrecipitation", True),
                "maxPayloadKg": data.get("maxPayloadKg"),
                "inspectionProgram": data.get("inspectionProgram", {}),
            }
            self.store.aircraft[aid] = row
            self.store.save()
            return dict(row)

    def register_component(self, data: dict) -> dict:
        with self.store.lock:
            serial = data["serial"]
            if serial in self.store.components:
                raise ConflictError(f"部件序列号已存在: {serial}")
            row = {
                "serial": serial,
                "kind": data["kind"],
                "model": data["model"],
                "cycleLimit": data.get("cycleLimit"),
                "hourLimit": data.get("hourLimit"),
                "inspectionEveryCycles": data.get("inspectionEveryCycles"),
                "inspectionEveryHours": data.get("inspectionEveryHours"),
            }
            self.store.components[serial] = row
            self.store.save()
            return dict(row)

    def register_firmware(self, data: dict) -> dict:
        with self.store.lock:
            fid = data["id"]
            if fid in self.store.firmware:
                raise ConflictError(f"固件已存在: {fid}")
            row = {
                "id": fid,
                "version": data["version"],
                "targetKind": data["targetKind"],
                "approved": bool(data.get("approved", True)),
            }
            self.store.firmware[fid] = row
            self.store.save()
            return dict(row)

    # ========================================================== 拆装履历
    def install_component(
        self,
        aircraft_id: str,
        position: str,
        serial: str,
        at: str,
        actor_id: str,
        baseline_cycles: Optional[float] = None,
        baseline_hours: Optional[float] = None,
    ) -> dict:
        """装机：形成连续履历。同装配位旧件在 at 时刻拆除，区间不得重叠。"""
        with self.store.lock:
            actor = self._person(actor_id)
            self._require_role(actor, {ROLE_MECHANIC, ROLE_REGULATOR})
            aircraft = self._aircraft(aircraft_id)
            component = self._component(serial)
            ts = timeutil.parse(at)
            if position not in aircraft.required_positions:
                raise ValidationError(f"机体无此装配位: {position}")
            if aircraft.required_positions[position] != component.kind:
                raise ValidationError(
                    f"装配位 {position} 需要 {aircraft.required_positions[position]}，"
                    f"收到 {component.kind}"
                )
            # 部件同一时刻只能在一个装机区间内
            for r in self.store.installs:
                if r["componentSerial"] != serial:
                    continue
                start = timeutil.parse(r["installedAt"])
                end = timeutil.parse(r["removedAt"]) if r.get("removedAt") else None
                if end is None or start <= ts < end:
                    raise ConflictError(
                        f"部件 {serial} 在 {_iso(ts)} 仍处于在翼状态（安装记录 {r['id']}）"
                    )
            # 同机体同装配位的在翼旧件：先拆除；区间严格不重叠
            for r in self.store.installs:
                if r["aircraftId"] != aircraft_id or r["position"] != position:
                    continue
                if r.get("removedAt") is None:
                    installed = timeutil.parse(r["installedAt"])
                    if ts < installed:
                        raise ConflictError("拆装时间早于当前在翼件装机时间，履历不得倒挂")
                    r["removedAt"] = _iso(ts)
                    r["removedBy"] = actor_id
                else:
                    if timeutil.parse(r["installedAt"]) <= ts < timeutil.parse(r["removedAt"]):
                        raise ConflictError("装机区间与既有履历重叠，同一装配位不得有两个有效配置")
            # 有寿命限制的单位必须同步基线读数（“换电池未同步循环寿命”在此拦截）
            if component.cycle_limit is not None and baseline_cycles is None:
                raise ValidationError(f"部件 {serial} 有循环寿命限制，装机必须同步循环读数")
            if component.hour_limit is not None and baseline_hours is None:
                raise ValidationError(f"部件 {serial} 有小时寿命限制，装机必须同步小时读数")
            rec_id = self.store.next_id("install")
            row = {
                "id": rec_id,
                "aircraftId": aircraft_id,
                "position": position,
                "componentSerial": serial,
                "installedAt": _iso(ts),
                "removedAt": None,
                "baselineCycles": baseline_cycles,
                "baselineHours": baseline_hours,
                "installedBy": actor_id,
                "removedBy": None,
            }
            self.store.installs.append(row)
            self.store.save()
            return dict(row)

    def remove_component(self, aircraft_id: str, position: str, at: str, actor_id: str) -> dict:
        with self.store.lock:
            actor = self._person(actor_id)
            self._require_role(actor, {ROLE_MECHANIC, ROLE_REGULATOR})
            ts = timeutil.parse(at)
            for r in self.store.installs:
                if (
                    r["aircraftId"] == aircraft_id
                    and r["position"] == position
                    and r.get("removedAt") is None
                ):
                    if ts < timeutil.parse(r["installedAt"]):
                        raise ConflictError("拆除时间早于装机时间")
                    r["removedAt"] = _iso(ts)
                    r["removedBy"] = actor_id
                    self.store.save()
                    return dict(r)
            raise NotFoundError(f"装配位 {position} 在该时刻无在翼部件")

    def flash_firmware(self, aircraft_id: str, firmware_id: str, at: str, actor_id: str) -> dict:
        with self.store.lock:
            actor = self._person(actor_id)
            self._require_role(actor, {ROLE_MECHANIC, ROLE_REGULATOR})
            aircraft = self._aircraft(aircraft_id)
            fw_row = self.store.firmware.get(firmware_id)
            if fw_row is None:
                raise NotFoundError(f"固件不存在: {firmware_id}")
            ts = timeutil.parse(at)
            target = fw_row["targetKind"]
            fitted = [
                (pos, kind) for pos, kind in aircraft.required_positions.items() if kind == target
            ]
            if not fitted:
                raise ValidationError(f"机体 {aircraft_id} 无 {target} 装配位，无法烧录该固件")
            rec_id = self.store.next_id("flash")
            row = {
                "id": rec_id,
                "aircraftId": aircraft_id,
                "firmwareId": firmware_id,
                "flashedAt": _iso(ts),
                "flashedBy": actor_id,
            }
            self.store.flashes.append(row)
            self.store.save()
            return dict(row)

    # ============================================================== 检查
    def record_inspection(self, data: dict, actor_id: str) -> dict:
        """登记检查事实；签字资质是否有效在放行评估时判定（过期签字必须留痕）。"""
        with self.store.lock:
            actor = self._person(actor_id)
            self._require_role(actor, {ROLE_MECHANIC, ROLE_REGULATOR})
            aircraft = self._aircraft(data["aircraftId"])
            signer = self._person(data["signedBy"])
            signed_at = timeutil.parse(data["signedAt"])
            valid_from = timeutil.parse(data.get("validFrom", data["signedAt"]))
            validity_days = data.get("validityDays")
            if validity_days is not None:
                from datetime import timedelta

                valid_until = valid_from + timedelta(days=float(validity_days))
            else:
                valid_until = timeutil.parse(data["validUntil"])
            if valid_until <= valid_from:
                raise ValidationError("检查有效期失效时间必须晚于生效时间")
            scope = data["scope"]
            program = aircraft.inspection_program
            if program and scope not in program:
                raise ValidationError(f"检查项目 {scope} 不在机体定检大纲内")
            row = {
                "id": self.store.next_id("insp"),
                "aircraftId": aircraft.id,
                "scope": scope,
                "signedBy": signer.id,
                "signedAt": _iso(signed_at),
                "validFrom": _iso(valid_from),
                "validUntil": _iso(valid_until),
                "componentSerial": data.get("componentSerial"),
                "result": data.get("result", "pass"),
                "recordedBy": actor_id,
            }
            self.store.inspections.append(row)
            self.store.save()
            return dict(row)

    # ============================================================== 工单
    def open_work_order(self, data: dict, actor_id: str) -> dict:
        with self.store.lock:
            actor = self._person(actor_id)
            self._require_role(actor, {ROLE_MECHANIC, ROLE_REGULATOR})
            aircraft = self._aircraft(data["aircraftId"])
            ts = timeutil.parse(data.get("openedAt") or _iso(self._now()))
            row = {
                "id": self.store.next_id("wo"),
                "aircraftId": aircraft.id,
                "title": data["title"],
                "openedAt": _iso(ts),
                "closedAt": None,
                "createdBy": actor_id,
            }
            self.store.work_orders.append(row)
            self.store.save()
            return dict(row)

    def close_work_order(self, work_order_id: str, at: str, actor_id: str) -> dict:
        with self.store.lock:
            actor = self._person(actor_id)
            self._require_role(actor, {ROLE_MECHANIC, ROLE_REGULATOR})
            ts = timeutil.parse(at)
            for r in self.store.work_orders:
                if r["id"] == work_order_id:
                    if r.get("closedAt"):
                        raise ConflictError("工单已关闭")
                    if ts < timeutil.parse(r["openedAt"]):
                        raise ValidationError("关闭时间早于开工时间")
                    r["closedAt"] = _iso(ts)
                    self.store.save()
                    return dict(r)
            raise NotFoundError(f"工单不存在: {work_order_id}")

    # ============================================================== 任务
    def create_mission(self, data: dict, actor_id: str) -> dict:
        with self.store.lock:
            actor = self._person(actor_id)
            self._require_role(actor, {ROLE_RELEASE, ROLE_REGULATOR})
            aircraft = self._aircraft(data["aircraftId"])
            start = timeutil.parse(data["plannedStart"])
            end = timeutil.parse(data["plannedEnd"])
            if end <= start:
                raise ValidationError("任务结束时间必须晚于开始时间")
            mid = data["id"]
            if mid in self.store.missions:
                raise ConflictError(f"任务已存在: {mid}")
            row = {
                "id": mid,
                "aircraftId": aircraft.id,
                "plannedStart": _iso(start),
                "plannedEnd": _iso(end),
                "payload": data.get("payload", []),
                "forecast": data.get("forecast", {}),
                "status": "planned",
                "createdBy": actor_id,
            }
            self.store.missions[mid] = row
            self.store.save()
            return dict(row)

    # ====================================================== 有效配置/寿命
    def _install_at(self, aircraft_id: str, at) -> dict[str, InstallRecord]:
        """某时刻每个在翼装配位的安装记录。"""
        fitted: dict[str, InstallRecord] = {}
        for rec in self._installs(aircraft_id):
            if rec.covers(at):
                if rec.position in fitted:
                    # 数据层已保证不重叠；这里是最终防线
                    raise ConflictError(
                        f"机体 {aircraft_id} 在 {_iso(at)} 的 {rec.position} 位存在两个有效配置"
                    )
                fitted[rec.position] = rec
        return fitted

    def _flights_for_install(self, install: InstallRecord, until) -> list[FlightLog]:
        """归属到某次装机区间的飞行：以撤轮挡时刻所在区间为准（放行已保证
        任务窗口内配置唯一）。"""
        out = []
        for f in self._flights(install.aircraft_id):
            if f.off_block_at < install.installed_at:
                continue
            if install.removed_at is not None and f.off_block_at >= install.removed_at:
                continue
            if f.off_block_at > until:
                continue
            out.append(f)
        return out

    def component_life(self, serial: str, at) -> dict:
        """部件截至 at 的累计寿命：装机同步基线 + 在翼期间实际飞行。"""
        with self.store.lock:
            return self._component_life_locked(serial, at)

    def _component_life_locked(self, serial: str, at) -> dict:
        at = timeutil.parse(at) if not hasattr(at, "year") else at
        component = self._component(serial)
        cycles = hours = None
        baseline_c = baseline_h = None
        current_install_id = None
        for rec in sorted(
            [r for r in self.store.installs if r["componentSerial"] == serial],
            key=lambda r: r["installedAt"],
        ):
            raw_removed = rec.get("removedAt")
            inst = InstallRecord(
                id=rec["id"],
                aircraft_id=rec["aircraftId"],
                position=rec["position"],
                component_serial=serial,
                installed_at=timeutil.parse(rec["installedAt"]),
                removed_at=timeutil.parse(raw_removed) if raw_removed else None,
                baseline_cycles=rec.get("baselineCycles"),
                baseline_hours=rec.get("baselineHours"),
            )
            if at < inst.installed_at:
                break
            baseline_c = inst.baseline_cycles
            baseline_h = inst.baseline_hours
            end = min(at, inst.removed_at) if inst.removed_at else at
            flights = self._flights_for_install(inst, end)
            cycles = (baseline_c or 0.0) + sum(f.cycles for f in flights)
            hours = (baseline_h or 0.0) + sum(f.hours for f in flights)
            if inst.removed_at is None or at < inst.removed_at:
                current_install_id = inst.id
        return {
            "serial": serial,
            "kind": component.kind,
            "at": _iso(at),
            "cycles": cycles,
            "hours": hours,
            "cycleLimit": component.cycle_limit,
            "hourLimit": component.hour_limit,
            "baselineCycles": baseline_c,
            "baselineHours": baseline_h,
            "installId": current_install_id,
        }

    def airframe_life(self, aircraft_id: str, at) -> dict:
        with self.store.lock:
            aircraft = self._aircraft(aircraft_id)
            flights = [f for f in self._flights(aircraft_id) if f.off_block_at <= at]
            return {
                "aircraftId": aircraft_id,
                "at": _iso(at),
                "cycles": sum(f.cycles for f in flights),
                "hours": sum(f.hours for f in flights),
                "cycleLimit": aircraft.airframe_cycle_limit,
                "hourLimit": aircraft.airframe_hour_limit,
            }

    def effective_config(self, aircraft_id: str, at) -> dict:
        """指定时刻的有效配置：在翼部件 + 当时寿命 + 生效固件。历史时刻可回溯。"""
        with self.store.lock:
            return self._effective_config_locked(aircraft_id, at)

    def _effective_config_locked(self, aircraft_id: str, at) -> dict:
        aircraft = self._aircraft(aircraft_id)
        at = timeutil.parse(at) if not hasattr(at, "year") else at
        positions = {}
        for position, kind in aircraft.required_positions.items():
            rec = self._install_at(aircraft_id, at).get(position)
            if rec is None:
                positions[position] = {"requiredKind": kind, "fitted": False}
                continue
            life = self.component_life(rec.component_serial, at)
            positions[position] = {
                "requiredKind": kind,
                "fitted": True,
                "serial": rec.component_serial,
                "installId": rec.id,
                "installedAt": _iso(rec.installed_at),
                "baselineCycles": rec.baseline_cycles,
                "baselineHours": rec.baseline_hours,
                "accruedCycles": life["cycles"],
                "accruedHours": life["hours"],
                "cycleLimit": life["cycleLimit"],
                "hourLimit": life["hourLimit"],
            }
        firmware = None
        flashes = sorted(
            (r for r in self.store.flashes if r["aircraftId"] == aircraft_id),
            key=lambda r: r["flashedAt"],
        )
        for f in flashes:
            if timeutil.parse(f["flashedAt"]) <= at:
                fw = self.store.firmware[f["firmwareId"]]
                firmware = {
                    "flashId": f["id"],
                    "firmwareId": fw["id"],
                    "version": fw["version"],
                    "targetKind": fw["targetKind"],
                    "approved": fw["approved"],
                    "flashedAt": f["flashedAt"],
                }
        return {"aircraftId": aircraft_id, "at": _iso(at), "positions": positions, "firmware": firmware}

    # ========================================================== 放行评估
    def _signature_basis(self, aircraft: Aircraft, insp: Inspection) -> dict:
        """签字依据：签字人在签字时刻是否具备匹配且有效的资质。"""
        try:
            signer = self._person(insp.signed_by)
        except NotFoundError:
            return {
                "inspectionId": insp.id,
                "scope": insp.scope,
                "signedBy": insp.signed_by,
                "signedAt": _iso(insp.signed_at),
                "valid": False,
                "reason": "签字人不存在",
            }
        cert = signer.cert_for(insp.scope, insp.signed_at)
        return {
            "inspectionId": insp.id,
            "scope": insp.scope,
            "signedBy": signer.id,
            "signedByName": signer.name,
            "signedAt": _iso(insp.signed_at),
            "valid": cert is not None,
            "certificationId": cert.id if cert else None,
            "certValidFrom": _iso(cert.valid_from) if cert else None,
            "certValidUntil": _iso(cert.valid_until) if cert else None,
            "reason": None if cert else "签字时刻无对应 scope 的有效资质（资质过期或不匹配）",
            "validFrom": _iso(insp.valid_from),
            "validUntil": _iso(insp.valid_until),
            "componentSerial": insp.component_serial,
        }

    def _evaluate(self, mission: Mission, actor: Person) -> ReleaseEvaluation:
        aircraft = self._aircraft(mission.aircraft_id)
        start, end = mission.planned_start, mission.planned_end
        blockers: list[dict] = []
        life_basis: list[dict] = []
        signature_basis: list[dict] = []

        # ---- 配置完整性与窗口内唯一性
        config_start = self.effective_config(aircraft.id, start)
        for position, info in config_start["positions"].items():
            if not info["fitted"]:
                blockers.append({
                    "code": "CONFIG_POSITION_VACANT",
                    "position": position,
                    "message": f"必需装配位 {position} 空缺",
                })
        # 窗口内是否发生拆装（同一机体出现两个有效配置）
        mid_changes = [
            r for r in self.store.installs
            if r["aircraftId"] == aircraft.id
            and start < timeutil.parse(r["installedAt"]) < end
        ]
        mid_changes += [
            r for r in self.store.installs
            if r["aircraftId"] == aircraft.id
            and r.get("removedAt")
            and start < timeutil.parse(r["removedAt"]) < end
        ]
        if mid_changes:
            blockers.append({
                "code": "CONFIG_CHANGES_DURING_WINDOW",
                "message": "任务窗口内存在拆装记录，窗口内有效配置不唯一",
                "installIds": sorted({r["id"] for r in mid_changes}),
            })

        # ---- 部件寿命（同步依据 + 到限）
        for position, info in config_start["positions"].items():
            if not info["fitted"]:
                continue
            serial = info["serial"]
            component = self._component(serial)
            rec = self._install_at(aircraft.id, start)[position]
            life = self.component_life(serial, start)
            entry = {
                "position": position,
                "serial": serial,
                "kind": component.kind,
                "baselineCycles": rec.baseline_cycles,
                "baselineHours": rec.baseline_hours,
                "accruedCycles": life["cycles"],
                "accruedHours": life["hours"],
                "cycleLimit": component.cycle_limit,
                "hourLimit": component.hour_limit,
            }
            life_basis.append(entry)
            if component.cycle_limit is not None and rec.baseline_cycles is None:
                blockers.append({
                    "code": "LIFE_BASELINE_MISSING",
                    "position": position,
                    "serial": serial,
                    "unit": "cycle",
                    "message": f"部件 {serial} 循环寿命未同步，累计循环无溯源依据",
                })
            if component.hour_limit is not None and rec.baseline_hours is None:
                blockers.append({
                    "code": "LIFE_BASELINE_MISSING",
                    "position": position,
                    "serial": serial,
                    "unit": "hour",
                    "message": f"部件 {serial} 小时寿命未同步，累计小时无溯源依据",
                })
            if (
                component.cycle_limit is not None
                and rec.baseline_cycles is not None
                and life["cycles"] is not None
                and life["cycles"] >= component.cycle_limit
            ):
                blockers.append({
                    "code": "LIFE_LIMIT_EXCEEDED",
                    "position": position,
                    "serial": serial,
                    "unit": "cycle",
                    "used": life["cycles"],
                    "limit": component.cycle_limit,
                    "message": f"部件 {serial} 循环寿命到限 "
                               f"({life['cycles']}/{component.cycle_limit})",
                })
            if (
                component.hour_limit is not None
                and rec.baseline_hours is not None
                and life["hours"] is not None
                and life["hours"] >= component.hour_limit
            ):
                blockers.append({
                    "code": "LIFE_LIMIT_EXCEEDED",
                    "position": position,
                    "serial": serial,
                    "unit": "hour",
                    "used": life["hours"],
                    "limit": component.hour_limit,
                    "message": f"部件 {serial} 小时寿命到限 "
                               f"({life['hours']}/{component.hour_limit})",
                })

        # ---- 机体寿命
        af = self.airframe_life(aircraft.id, start)
        if aircraft.airframe_cycle_limit and af["cycles"] >= aircraft.airframe_cycle_limit:
            blockers.append({
                "code": "AIRFRAME_LIFE_EXCEEDED",
                "unit": "cycle",
                "used": af["cycles"],
                "limit": aircraft.airframe_cycle_limit,
                "message": f"机体循环寿命到限 ({af['cycles']}/{aircraft.airframe_cycle_limit})",
            })
        if aircraft.airframe_hour_limit and af["hours"] >= aircraft.airframe_hour_limit:
            blockers.append({
                "code": "AIRFRAME_LIFE_EXCEEDED",
                "unit": "hour",
                "used": af["hours"],
                "limit": aircraft.airframe_hour_limit,
                "message": f"机体小时寿命到限 ({af['hours']}/{aircraft.airframe_hour_limit})",
            })

        # ---- 固件
        fw = config_start["firmware"]
        needs_fc = KIND_FLIGHT_CONTROLLER in aircraft.required_positions.values()
        if needs_fc:
            if fw is None:
                blockers.append({"code": "FIRMWARE_MISSING", "message": "飞控无生效固件烧录记录"})
            elif not fw["approved"]:
                blockers.append({
                    "code": "FIRMWARE_NOT_APPROVED",
                    "firmwareId": fw["firmwareId"],
                    "version": fw["version"],
                    "message": f"固件 {fw['version']} 未获批准",
                })

        # ---- 定检与签字资质
        inspections = self._inspections(aircraft.id)
        for scope, _validity_days in aircraft.inspection_program.items():
            candidates = [
                i for i in inspections
                if i.scope == scope and i.result == "pass"
                and i.valid_from <= start and i.valid_until > start
            ]
            # 部件专属检查（如螺旋桨）必须覆盖当前在翼件
            position_for_kind = None
            scoped_serial = None
            for pos, info in config_start["positions"].items():
                if info["fitted"] and scope.startswith(info["requiredKind"]):
                    position_for_kind, scoped_serial = pos, info["serial"]
            if scoped_serial is not None:
                matching = [i for i in candidates if i.component_serial in (None, scoped_serial)]
                # 同 scope 曾给旧件做过的检查不能覆盖新件
                candidates = matching
            if not candidates:
                blockers.append({
                    "code": "INSPECTION_DUE",
                    "scope": scope,
                    "serial": scoped_serial,
                    "message": f"检查项目 {scope} 到期或缺失"
                               + (f"（部件 {scoped_serial}）" if scoped_serial else ""),
                })
                continue
            # 取最新一次，逐条给出签字依据
            latest = max(candidates, key=lambda i: i.signed_at)
            basis = self._signature_basis(aircraft, latest)
            signature_basis.append(basis)
            if not basis["valid"]:
                blockers.append({
                    "code": "INSPECTION_SIGNATURE_INVALID",
                    "scope": scope,
                    "serial": scoped_serial,
                    "inspectionId": latest.id,
                    "signedBy": latest.signed_by,
                    "message": f"检查 {scope} 由资质过期/不符人员 {latest.signed_by} 签字",
                })

        # ---- 维修占用
        for wo in self._work_orders(aircraft.id):
            if wo.overlaps(start, end):
                blockers.append({
                    "code": "MAINTENANCE_OCCUPANCY",
                    "workOrderId": wo.id,
                    "message": f"维修工单 {wo.id}（{wo.title}）与任务窗口并发占用同一机体",
                })

        # ---- 任务并发（同一机体不得同时执行两个任务）
        for other_id, row in self.store.missions.items():
            if other_id == mission.id:
                continue
            if row["aircraftId"] != aircraft.id or row.get("status") in ("cancelled", "completed"):
                continue
            o_start = timeutil.parse(row["plannedStart"])
            o_end = timeutil.parse(row["plannedEnd"])
            if start < o_end and end > o_start:
                blockers.append({
                    "code": "MISSION_CONFLICT",
                    "otherMissionId": other_id,
                    "message": f"与任务 {other_id} 的计划窗口重叠，同一机体不能并行执行",
                })

        # ---- 载荷
        total_kg = sum(float(item.get("kg", 0)) for item in mission.payload)
        if aircraft.max_payload_kg is not None and total_kg > aircraft.max_payload_kg:
            blockers.append({
                "code": "PAYLOAD_OVERWEIGHT",
                "used": total_kg,
                "limit": aircraft.max_payload_kg,
                "message": f"任务载荷 {total_kg}kg 超出 {aircraft.max_payload_kg}kg 限制",
            })

        # ---- 环境包线
        forecast = mission.forecast or {}
        if "windKt" in forecast and aircraft.max_wind_kt is not None:
            if float(forecast["windKt"]) > aircraft.max_wind_kt:
                blockers.append({
                    "code": "ENV_WIND",
                    "used": forecast["windKt"],
                    "limit": aircraft.max_wind_kt,
                    "message": f"预报风速 {forecast['windKt']}kt 超过限制 {aircraft.max_wind_kt}kt",
                })
        if "tempC" in forecast:
            temp = float(forecast["tempC"])
            if aircraft.min_temp_c is not None and temp < aircraft.min_temp_c:
                blockers.append({
                    "code": "ENV_TEMP",
                    "used": temp,
                    "limit": aircraft.min_temp_c,
                    "message": f"预报温度 {temp}℃ 低于下限 {aircraft.min_temp_c}℃",
                })
            if aircraft.max_temp_c is not None and temp > aircraft.max_temp_c:
                blockers.append({
                    "code": "ENV_TEMP",
                    "used": temp,
                    "limit": aircraft.max_temp_c,
                    "message": f"预报温度 {temp}℃ 高于上限 {aircraft.max_temp_c}℃",
                })
        if forecast.get("precip") and not aircraft.allow_precipitation:
            blockers.append({"code": "ENV_PRECIP", "message": "预报有降水，机体不允许降水条件飞行"})

        return ReleaseEvaluation(
            id=self.store.next_id("eval"),
            mission_id=mission.id,
            aircraft_id=aircraft.id,
            effective_at=start,
            evaluated_at=self._now(),
            actor_id=actor.id,
            config=config_start,
            blockers=blockers,
            life_basis=life_basis,
            signature_basis=signature_basis,
            airframe_life=af,
        )

    def evaluate_release(self, mission_id: str, actor_id: str) -> dict:
        """评估放行：无阻断 GO；否则 NO_GO 并形成拦截结论。

        若存在对本任务有效的紧急放行（监管人员批准、失效条件均未触发），
        且阻断项全部属于可豁免类型，则结论 EMERGENCY_GO。
        """
        with self.store.lock:
            actor = self._person(actor_id)
            self._require_role(actor, {ROLE_RELEASE, ROLE_REGULATOR})
            mission = self._mission(mission_id)
            if mission.status in ("cancelled", "completed"):
                raise ConflictError(f"任务状态为 {mission.status}，不再评估放行")
            evaluation = self._evaluate(mission, actor)

            decision = "GO" if not evaluation.blockers else "NO_GO"
            emergency_id = None
            if evaluation.blockers:
                er = self._find_valid_emergency(mission, evaluation.blocker_codes())
                if er is not None:
                    decision = "EMERGENCY_GO"
                    emergency_id = er.id
                    er_row = self.store.emergency_releases[er.id]
                    er_row["usedReleaseIds"].append(evaluation.id)
            evaluation.decision = decision
            evaluation.emergency_release_id = emergency_id

            row = self._evaluation_to_row(evaluation)
            self.store.evaluations[evaluation.id] = row
            self.store.missions[mission.id]["lastEvaluationId"] = evaluation.id
            if decision in ("GO", "EMERGENCY_GO"):
                self.store.missions[mission.id]["status"] = "released"
            else:
                self.store.missions[mission.id]["status"] = "blocked"
            self.store.save()
            return row

    def _find_valid_emergency(
        self, mission: Mission, blocker_codes: set[str]
    ) -> Optional[EmergencyRelease]:
        for row in self.store.emergency_releases.values():
            if row["aircraftId"] != mission.aircraft_id:
                continue
            er = self._hydrate_emergency(row)
            if er.valid_for(mission.id, self._now(), blocker_codes) is None:
                return er
        return None

    def _hydrate_emergency(self, row: dict) -> EmergencyRelease:
        return EmergencyRelease(
            id=row["id"],
            aircraft_id=row["aircraftId"],
            mission_id=row["missionId"],
            approved_by=row["approvedBy"],
            approver_role=row["approverRole"],
            created_at=timeutil.parse(row["createdAt"]),
            valid_until=timeutil.parse(row["validUntil"]),
            max_flights=int(row["maxFlights"]),
            permitted_blockers=list(row["permittedBlockers"]),
            reason=row.get("reason", ""),
            used_flight_ids=list(row.get("usedReleaseIds", [])),
            revoked=row.get("revoked", False),
        )

    def _evaluation_to_row(self, e: ReleaseEvaluation) -> dict:
        return {
            "id": e.id,
            "missionId": e.mission_id,
            "aircraftId": e.aircraft_id,
            "effectiveAt": _iso(e.effective_at),
            "evaluatedAt": _iso(e.evaluated_at),
            "actorId": e.actor_id,
            "decision": e.decision,
            "blockers": e.blockers,
            "config": e.config,
            "lifeBasis": e.life_basis,
            "signatureBasis": e.signature_basis,
            "airframeLife": e.airframe_life,
            "emergencyReleaseId": e.emergency_release_id,
            "basisRevisedAfter": e.basis_revised_after,
        }

    def grant_emergency_release(self, mission_id: str, data: dict, actor_id: str) -> dict:
        """紧急放行：仅独立角色（监管人员）可批准，必须携带明确失效条件。"""
        with self.store.lock:
            actor = self._person(actor_id)
            # 独立角色：放行员/机务均不得批准自己相关的紧急放行
            if actor.role != ROLE_REGULATOR:
                raise AuthorizationError("紧急放行只能由独立角色（监管人员）批准")
            mission = self._mission(mission_id)
            valid_until = timeutil.parse(data["validUntil"])
            if valid_until <= self._now():
                raise ValidationError("紧急放行失效时刻必须晚于当前时刻")
            max_flights = int(data.get("maxFlights", 1))
            if max_flights < 1:
                raise ValidationError("紧急放行至少允许 1 次飞行")
            permitted = list(data.get("permittedBlockers", sorted(OVERRIDABLE_BLOCKERS)))
            illegal = sorted(set(permitted) - OVERRIDABLE_BLOCKERS)
            if illegal:
                raise ValidationError(f"以下硬性阻断不得被紧急放行覆盖: {illegal}")
            if not data.get("reason"):
                raise ValidationError("紧急放行必须填写批准理由")
            row = {
                "id": self.store.next_id("er"),
                "aircraftId": mission.aircraft_id,
                "missionId": mission.id,
                "approvedBy": actor.id,
                "approverRole": actor.role,
                "createdAt": _iso(self._now()),
                "validUntil": _iso(valid_until),
                "maxFlights": max_flights,
                "permittedBlockers": permitted,
                "reason": data["reason"],
                "usedReleaseIds": [],
                "revoked": False,
            }
            self.store.emergency_releases[row["id"]] = row
            self.store.save()
            return dict(row)

    def revoke_emergency_release(self, release_id: str, actor_id: str) -> dict:
        with self.store.lock:
            actor = self._person(actor_id)
            self._require_role(actor, {ROLE_REGULATOR})
            row = self.store.emergency_releases.get(release_id)
            if row is None:
                raise NotFoundError(f"紧急放行不存在: {release_id}")
            row["revoked"] = True
            self.store.save()
            return dict(row)

    # ========================================================== 飞行日志
    def ingest_flight_log(self, data: dict, actor_id: str, received_at: Optional[str] = None) -> dict:
        """受理飞行日志（可迟到）。受理时固化当时有效配置快照，并圈定追溯影响。"""
        with self.store.lock:
            actor = self._person(actor_id)
            self._require_role(actor, {ROLE_MECHANIC, ROLE_REGULATOR})
            aircraft = self._aircraft(data["aircraftId"])
            off_block = timeutil.parse(data["offBlockAt"])
            on_block = timeutil.parse(data["onBlockAt"])
            if on_block <= off_block:
                raise ValidationError("上轮挡时间必须晚于撤轮挡时间")
            received = (
                timeutil.parse(received_at)
                if received_at
                else timeutil.parse(data["receivedAt"])
                if data.get("receivedAt")
                else self._now()
            )
            if received < on_block:
                raise ValidationError("受理时间不能早于飞行落地时间")
            log_id = data.get("id") or self.store.next_id("flight")
            if any(r["id"] == log_id for r in self.store.flight_logs):
                raise ConflictError(f"飞行日志已存在: {log_id}")
            late = (received - on_block).total_seconds() > self.late_grace_seconds
            # 受理时刻固化配置快照（纯数据副本，此后拆装不改变它）
            snapshot = self.effective_config(aircraft.id, off_block)
            row = {
                "id": log_id,
                "aircraftId": aircraft.id,
                "offBlockAt": _iso(off_block),
                "onBlockAt": _iso(on_block),
                "cycles": float(data.get("cycles", 1)),
                "hours": float(data["hours"]),
                "receivedAt": _iso(received),
                "missionId": data.get("missionId"),
                "payload": data.get("payload", []),
                "weather": data.get("weather", {}),
                "configSnapshot": snapshot,
                "late": late,
                "recordedBy": actor_id,
            }
            self.store.flight_logs.append(row)
            self.store.save()
            impact = self._impact_for_log(row)
            return {"flightLog": dict(row), "impact": impact}

    def _threshold_crossings(self, before: float, after: float, every: float) -> list[float]:
        import math

        if every <= 0:
            return []
        first = math.floor(before / every) + 1
        last = math.floor(after / every)
        return [n * every for n in range(int(first), int(last) + 1)]

    def _impact_for_log(self, row: dict) -> dict:
        """补计一条日志后，圈定受影响任务、放行结论与复检范围。"""
        aircraft_id = row["aircraftId"]
        off_block = timeutil.parse(row["offBlockAt"])
        on_block = timeutil.parse(row["onBlockAt"])
        received = timeutil.parse(row["receivedAt"])
        added_c, added_h = float(row["cycles"]), float(row["hours"])

        components_impact = []
        for pos in self._install_at(aircraft_id, off_block).values():
            comp = self._component(pos.component_serial)
            after = self.component_life(comp.serial, on_block)
            before_c = (None if after["cycles"] is None else after["cycles"] - added_c)
            before_h = (None if after["hours"] is None else after["hours"] - added_h)
            reinspect = []
            if comp.inspection_every_cycles and before_c is not None:
                for crossed in self._threshold_crossings(before_c, after["cycles"], comp.inspection_every_cycles):
                    reinspect.append({
                        "scope": f"{comp.kind}_cycle_inspection",
                        "serial": comp.serial,
                        "unit": "cycle",
                        "threshold": crossed,
                    })
            if comp.inspection_every_hours and before_h is not None:
                for crossed in self._threshold_crossings(before_h, after["hours"], comp.inspection_every_hours):
                    reinspect.append({
                        "scope": f"{comp.kind}_hour_inspection",
                        "serial": comp.serial,
                        "unit": "hour",
                        "threshold": crossed,
                    })
            newly_limited = []
            if comp.cycle_limit is not None and before_c is not None and after["cycles"] >= comp.cycle_limit:
                newly_limited.append({"unit": "cycle", "limit": comp.cycle_limit, "used": after["cycles"]})
            if comp.hour_limit is not None and before_h is not None and after["hours"] >= comp.hour_limit:
                newly_limited.append({"unit": "hour", "limit": comp.hour_limit, "used": after["hours"]})
            components_impact.append({
                "position": pos.position,
                "serial": comp.serial,
                "kind": comp.kind,
                "beforeCycles": before_c,
                "afterCycles": after["cycles"],
                "beforeHours": before_h,
                "afterHours": after["hours"],
                "newlyAtLimit": newly_limited,
                "reinspections": reinspect,
            })

        # 历史放行评估：决策在日志受理之前做出，而飞行发生在其放行生效时刻
        # 之前——即该飞行本应计入寿命依据却被遗漏，需要复核。
        evaluations = []
        for eval_id, ev in self.store.evaluations.items():
            if ev["aircraftId"] != aircraft_id:
                continue
            decided = timeutil.parse(ev["evaluatedAt"])
            effective_at = timeutil.parse(ev["effectiveAt"])
            if (
                decided < received
                and off_block < effective_at
                and ev["decision"] in ("GO", "EMERGENCY_GO")
            ):
                evaluations.append({
                    "evaluationId": eval_id,
                    "missionId": ev["missionId"],
                    "decision": ev["decision"],
                    "reason": "放行决策时该飞行已实际发生但日志尚未受理，寿命/配置依据不完整",
                })
                ev["basisRevisedAfter"] = True

        # 受影响任务：窗口晚于飞行发生、尚未终结的任务，以及上述评估对应任务
        affected_ids = {x["missionId"] for x in evaluations}
        future = []
        for mid, m in self.store.missions.items():
            if m["aircraftId"] != aircraft_id:
                continue
            if m.get("status") in ("cancelled", "completed"):
                continue
            if timeutil.parse(m["plannedStart"]) >= off_block:
                future.append({"missionId": mid, "status": m.get("status"), "needsReevaluation": True})
                affected_ids.add(mid)

        af_after = self.airframe_life(aircraft_id, on_block)
        return {
            "aircraftId": aircraft_id,
            "flightLogId": row["id"],
            "late": row["late"],
            "occurredAt": row["offBlockAt"],
            "receivedAt": row["receivedAt"],
            "addedCycles": added_c,
            "addedHours": added_h,
            "components": components_impact,
            "airframe": {
                "afterCycles": af_after["cycles"],
                "afterHours": af_after["hours"],
                "cycleLimit": af_after["cycleLimit"],
                "hourLimit": af_after["hourLimit"],
            },
            "pastReleaseEvaluationsToReview": evaluations,
            "missionsToReevaluate": future,
            "affectedMissionIds": sorted(affected_ids),
        }

    def retro_impact(self, actor_id: str, since: Optional[str] = None) -> dict:
        """汇总某受理时间之后所有日志（默认全部迟到日志）的追溯影响。"""
        with self.store.lock:
            actor = self._person(actor_id)
            self._require_role(actor, {ROLE_MECHANIC, ROLE_REGULATOR})
            since_dt = timeutil.parse(since) if since else None
            logs = [
                r for r in self.store.flight_logs
                if (since_dt is None or timeutil.parse(r["receivedAt"]) >= since_dt)
                and (since_dt is not None or r.get("late"))
            ]
            return {"flightLogIds": [r["id"] for r in logs], "impacts": [self._impact_for_log(r) for r in logs]}

    # ========================================================== 岗位视图
    def mechanic_view(self, aircraft_id: str, at: str, actor_id: str) -> dict:
        """机务视图：有效配置、寿命读数与同步基线、检查签字依据。"""
        with self.store.lock:
            actor = self._person(actor_id)
            self._require_role(actor, {ROLE_MECHANIC, ROLE_REGULATOR})
            at_dt = timeutil.parse(at)
            aircraft = self._aircraft(aircraft_id)
            config = self.effective_config(aircraft_id, at_dt)
            sig_basis = [
                self._signature_basis(aircraft, insp)
                for insp in self._inspections(aircraft_id)
                if insp.current_at(at_dt)
            ]
            open_wo = [
                {"workOrderId": wo.id, "title": wo.title, "openedAt": _iso(wo.opened_at)}
                for wo in self._work_orders(aircraft_id) if wo.active_at(at_dt)
            ]
            return {
                "view": "mechanic",
                "aircraftId": aircraft_id,
                "at": _iso(at_dt),
                "effectiveConfig": config,
                "airframeLife": self.airframe_life(aircraft_id, at_dt),
                "signatureBasis": sig_basis,
                "openWorkOrders": open_wo,
            }

    def dispatch_view(self, mission_id: str, actor_id: str) -> dict:
        """调度视图：只给可执行结论与阻断项，不含寿命数值与资质细节。"""
        with self.store.lock:
            actor = self._person(actor_id)
            self._require_role(actor, {ROLE_RELEASE, ROLE_REGULATOR})
            mission = self._mission(mission_id)
            eval_row = None
            if self.store.missions[mission.id].get("lastEvaluationId"):
                eval_row = self.store.evaluations[self.store.missions[mission.id]["lastEvaluationId"]]
            if eval_row is None:
                return {
                    "view": "dispatch",
                    "missionId": mission_id,
                    "aircraftId": mission.aircraft_id,
                    "status": mission.status,
                    "decision": "NOT_EVALUATED",
                    "blockers": [],
                }
            blockers = [
                {"code": b["code"], "message": b["message"]} for b in eval_row["blockers"]
            ]
            out = {
                "view": "dispatch",
                "missionId": mission_id,
                "aircraftId": mission.aircraft_id,
                "plannedWindow": {
                    "start": _iso(mission.planned_start),
                    "end": _iso(mission.planned_end),
                },
                "status": self.store.missions[mission.id]["status"],
                "decision": eval_row["decision"],
                "blockers": blockers,
            }
            if eval_row.get("emergencyReleaseId"):
                out["emergencyReleaseId"] = eval_row["emergencyReleaseId"]
            if eval_row.get("basisRevisedAfter"):
                # 调度只需知道结论依据已被迟到数据动摇，需要重新评估
                out["basisRevisedAfter"] = True
            return out

    def readonly_status(self, aircraft_id: str, actor_id: str) -> dict:
        with self.store.lock:
            actor = self._person(actor_id)
            self._require_role(actor, {ROLE_READONLY, ROLE_REGULATOR})
            self._aircraft(aircraft_id)
            released = blocked = planned = 0
            for m in self.store.missions.values():
                if m["aircraftId"] != aircraft_id:
                    continue
                if m.get("status") == "released":
                    released += 1
                elif m.get("status") == "blocked":
                    blocked += 1
                elif m.get("status") == "planned":
                    planned += 1
            return {
                "view": "readonly",
                "aircraftId": aircraft_id,
                "missions": {"released": released, "blocked": blocked, "planned": planned},
            }
