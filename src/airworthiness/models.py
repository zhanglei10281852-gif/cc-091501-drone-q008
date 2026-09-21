"""适航管理领域模型。

所有时间字段均为带时区的 datetime，序列化为 ISO 8601 字符串。
配置快照（ConfigSnapshot）在任务放行时创建，之后任何装配变更都不回写，
保证历史飞行引用的配置不可变。
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime
from typing import Any, ClassVar, Optional

from .timeutil import FAR_FUTURE, parse_ts


def _dump_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_dump_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _dump_value(item) for key, item in value.items()}
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _dump_value(getattr(value, f.name)) for f in fields(value)}
    return value


class Serializable:
    """dataclass 的 JSON 序列化支持；子类用 DT_FIELDS 声明时间字段。"""

    DT_FIELDS: ClassVar[tuple[str, ...]] = ()

    def to_dict(self) -> dict:
        return {f.name: _dump_value(getattr(self, f.name)) for f in fields(self)}

    @classmethod
    def from_dict(cls, data: dict):
        values = dict(data)
        for name in cls.DT_FIELDS:
            if values.get(name) is not None:
                values[name] = parse_ts(values[name], name)
        return cls(**values)


@dataclass
class Airframe(Serializable):
    """机体。positions 为 安装位置 -> 所需部件类型。"""

    id: str
    model: str
    serial: str
    positions: dict[str, str]
    approved_firmware: list[str]
    required_inspections: list[str]
    compatible_payloads: list[str]
    max_payload_kg: float
    max_wind_mps: float
    min_temp_c: float
    max_temp_c: float
    allow_precipitation: bool = False


@dataclass
class Part(Serializable):
    """可序列化部件。life_synced_at 为 None 表示寿命数据尚未同步。"""

    DT_FIELDS: ClassVar[tuple[str, ...]] = ("life_synced_at",)

    serial: str
    type: str
    model: str
    cycle_limit: Optional[int]
    hour_limit: Optional[float]
    inspection_interval_cycles: Optional[int]
    used_cycles: int = 0
    used_hours: float = 0.0
    life_synced_at: Optional[datetime] = None


@dataclass
class InstallEvent(Serializable):
    """部件拆装履历。removed_at 为 None 表示当前在装。"""

    DT_FIELDS: ClassVar[tuple[str, ...]] = ("installed_at", "removed_at")

    id: str
    airframe_id: str
    part_serial: str
    part_type: str
    position: str
    installed_at: datetime
    removed_at: Optional[datetime] = None


@dataclass
class FirmwareUpdate(Serializable):
    DT_FIELDS: ClassVar[tuple[str, ...]] = ("activated_at",)

    airframe_id: str
    version: str
    activated_at: datetime


@dataclass
class Qualification(Serializable):
    """人员资质，kind 与检查类别对应。"""

    DT_FIELDS: ClassVar[tuple[str, ...]] = ("valid_from", "valid_until")

    personnel_id: str
    kind: str
    valid_from: datetime
    valid_until: datetime

    def covers(self, at: datetime) -> bool:
        return self.valid_from <= at <= self.valid_until


@dataclass
class InspectionSignature(Serializable):
    DT_FIELDS: ClassVar[tuple[str, ...]] = ("signed_at", "valid_until")

    id: str
    airframe_id: str
    kind: str
    personnel_id: str
    signed_at: datetime
    result: str  # "pass" | "fail"
    valid_until: datetime


@dataclass
class WorkOrder(Serializable):
    DT_FIELDS: ClassVar[tuple[str, ...]] = ("opened_at", "closed_at")

    id: str
    airframe_id: str
    kind: str
    grounding: bool
    opened_at: datetime
    closed_at: Optional[datetime] = None

    def is_open_at(self, at: datetime) -> bool:
        return self.opened_at <= at and (self.closed_at is None or self.closed_at > at)


@dataclass
class Payload(Serializable):
    kind: str
    weight_kg: float


@dataclass
class Environment(Serializable):
    wind_mps: float
    temp_c: float
    precipitation: bool = False


@dataclass
class Mission(Serializable):
    DT_FIELDS: ClassVar[tuple[str, ...]] = ("planned_start", "planned_end", "dispatched_at")

    id: str
    airframe_id: str
    payload: Payload
    planned_start: datetime
    planned_end: datetime
    environment: Environment
    status: str = "scheduled"  # scheduled | dispatched | completed | cancelled
    config_id: Optional[str] = None
    dispatched_at: Optional[datetime] = None

    @classmethod
    def from_dict(cls, data: dict):
        values = dict(data)
        values["payload"] = Payload.from_dict(values["payload"])
        values["environment"] = Environment.from_dict(values["environment"])
        for name in cls.DT_FIELDS:
            if values.get(name) is not None:
                values[name] = parse_ts(values[name], name)
        return cls(**values)


@dataclass
class SnapshotPart(Serializable):
    """放行时刻部件状态的固化副本。"""

    serial: str
    type: str
    position: str
    used_cycles: int
    used_hours: float
    cycle_limit: Optional[int]
    hour_limit: Optional[float]


@dataclass
class ConfigSnapshot(Serializable):
    """放行时刻的有效配置快照，创建后不可变。"""

    DT_FIELDS: ClassVar[tuple[str, ...]] = ("as_of", "created_at")

    id: str
    airframe_id: str
    as_of: datetime
    created_at: datetime
    firmware_version: Optional[str]
    parts: list[SnapshotPart]

    @classmethod
    def from_dict(cls, data: dict):
        values = dict(data)
        values["parts"] = [SnapshotPart.from_dict(item) for item in values["parts"]]
        for name in cls.DT_FIELDS:
            if values.get(name) is not None:
                values[name] = parse_ts(values[name], name)
        return cls(**values)


@dataclass
class Occupancy(Serializable):
    """机体占用记录。kind 为 mission 或 maintenance；end 为 None 表示开放式。"""

    DT_FIELDS: ClassVar[tuple[str, ...]] = ("start", "end")

    id: str
    airframe_id: str
    kind: str
    ref_id: str
    start: datetime
    end: Optional[datetime] = None

    def overlaps(self, start: datetime, end: Optional[datetime]) -> bool:
        own_end = self.end if self.end is not None else FAR_FUTURE
        other_end = end if end is not None else FAR_FUTURE
        return self.start < other_end and start < own_end


@dataclass
class FlightLog(Serializable):
    DT_FIELDS: ClassVar[tuple[str, ...]] = ("started_at", "ended_at", "received_at")

    id: str
    mission_id: str
    airframe_id: str
    config_id: str
    started_at: datetime
    ended_at: datetime
    cycles: int
    hours: float
    received_at: datetime


@dataclass
class LifeAdjustment(Serializable):
    """部件寿命变动台账（飞行消耗或人工同步），用于回算任意时刻寿命。"""

    DT_FIELDS: ClassVar[tuple[str, ...]] = ("at",)

    id: str
    part_serial: str
    at: datetime
    delta_cycles: int
    delta_hours: float
    kind: str  # "flight" | "sync"
    ref_id: str
    note: str = ""


@dataclass
class EmergencyRelease(Serializable):
    """紧急放行：独立角色批准，带明确失效条件。"""

    DT_FIELDS: ClassVar[tuple[str, ...]] = ("approved_at", "expires_at")

    id: str
    airframe_id: str
    mission_id: Optional[str]
    requested_by: str
    approved_by: str
    approved_at: datetime
    expires_at: datetime
    overrides: list[str]
    max_additional_cycles: Optional[int]
    conditions: str
    revoked: bool = False


@dataclass
class Blocker:
    code: str
    message: str
    detail: dict = field(default_factory=dict)


@dataclass
class ReleaseDecision:
    """一次放行评估的结论与依据。"""

    airframe_id: str
    mission_id: Optional[str]
    evaluated_at: datetime
    status: str  # "released" | "blocked"
    via: str  # "standard" | "emergency"
    blockers: list[Blocker] = field(default_factory=list)
    life_basis: list[dict] = field(default_factory=list)
    signature_basis: list[dict] = field(default_factory=list)
    config_id: Optional[str] = None
    emergency: Optional[dict] = None
