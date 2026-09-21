"""领域模型：机体、可序列化部件、固件、工单、检查、任务、飞行日志、紧急放行。"""

from dataclasses import dataclass, field, asdict
from typing import Any, Optional

from . import timeutil

# 角色（与 reference/domain.json 对齐）
ROLE_RELEASE = "放行员"
ROLE_MECHANIC = "机务人员"
ROLE_REGULATOR = "监管人员"
ROLE_READONLY = "只读用户"
ROLES = [ROLE_RELEASE, ROLE_MECHANIC, ROLE_REGULATOR, ROLE_READONLY]

# 部件类别
KIND_BATTERY = "battery"
KIND_PROPELLER = "propeller"
KIND_MOTOR = "motor"
KIND_FLIGHT_CONTROLLER = "flight_controller"

# 寿命单位
UNIT_CYCLE = "cycle"
UNIT_HOUR = "hour"

# 可被紧急放行覆盖的阻断类型；其余为硬性阻断（结构缺失、寿命无同步依据、
# 固件未批准、占用冲突、环境越限、寿命已到限），任何人不得豁免。
OVERRIDABLE_BLOCKERS = frozenset(
    {
        "INSPECTION_DUE",            # 定期检查到期
        "INSPECTION_SIGNATURE_INVALID",  # 签字人资质过期/不符
    }
)


def _dt(value: Optional[str]):
    return timeutil.parse(value) if value is not None else None


@dataclass
class Certification:
    """人员资质：在 [valid_from, valid_until) 内可对 scope 类检查签字。"""

    id: str
    person_id: str
    scope: str
    valid_from: Any
    valid_until: Any

    def valid_at(self, at) -> bool:
        return self.valid_from <= at < self.valid_until

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "personId": self.person_id,
            "scope": self.scope,
            "validFrom": timeutil.format_value(self.valid_from),
            "validUntil": timeutil.format_value(self.valid_until),
        }


@dataclass
class Person:
    id: str
    name: str
    role: str
    certifications: list[Certification] = field(default_factory=list)

    def cert_for(self, scope: str, at) -> Optional[Certification]:
        for cert in self.certifications:
            if cert.scope == scope and cert.valid_at(at):
                return cert
        return None


@dataclass
class Aircraft:
    id: str
    model: str
    # 必需装配位：position -> 部件类别，如 {"battery": "battery", "fc": "flight_controller"}
    required_positions: dict[str, str]
    # 机体寿命限制
    airframe_cycle_limit: Optional[float] = None
    airframe_hour_limit: Optional[float] = None
    # 环境包线
    max_wind_kt: Optional[float] = None
    min_temp_c: Optional[float] = None
    max_temp_c: Optional[float] = None
    allow_precipitation: bool = True
    max_payload_kg: Optional[float] = None
    # 定检项目：检查 scope -> 有效期（天）。None 表示需要一次有效检查即可。
    inspection_program: dict[str, Optional[int]] = field(default_factory=dict)


@dataclass
class Component:
    """可序列化部件：寿命限制与定检间隔以循环/小时给出，None 表示该单位不适用。"""

    serial: str
    kind: str
    model: str
    cycle_limit: Optional[float] = None
    hour_limit: Optional[float] = None
    # 每隔多少循环/小时需要一次该部件类别检查（阈值跨越即圈定复检）
    inspection_every_cycles: Optional[float] = None
    inspection_every_hours: Optional[float] = None


@dataclass
class InstallRecord:
    """部件拆装履历条目：[installed_at, removed_at) 内部件位于某机体某装配位。

    baseline_cycles/baseline_hours 为装机时同步进来的既往寿命读数；
    缺少适用单位的基线意味着寿命无法溯源，放行会被拦截。
    """

    id: str
    aircraft_id: str
    position: str
    component_serial: str
    installed_at: Any
    removed_at: Optional[Any] = None
    baseline_cycles: Optional[float] = None
    baseline_hours: Optional[float] = None
    removed_by: Optional[str] = None

    @property
    def current(self) -> bool:
        return self.removed_at is None

    def covers(self, at) -> bool:
        return self.installed_at <= at and (self.removed_at is None or at < self.removed_at)


@dataclass
class FirmwareVersion:
    id: str
    version: str
    target_kind: str
    approved: bool = True


@dataclass
class FlashRecord:
    id: str
    aircraft_id: str
    firmware_id: str
    flashed_at: Any
    flashed_by: str


@dataclass
class Inspection:
    """检查/定检记录。有效期 [valid_from, valid_until)；签字须在签字时具备资质。"""

    id: str
    aircraft_id: str
    scope: str
    signed_by: str
    signed_at: Any
    valid_from: Any
    valid_until: Any
    component_serial: Optional[str] = None
    result: str = "pass"

    def current_at(self, at) -> bool:
        return self.valid_from <= at < self.valid_until and self.result == "pass"


@dataclass
class WorkOrder:
    """维修工单：[opened_at, closed_at) 内占用机体（未关闭则持续占用）。"""

    id: str
    aircraft_id: str
    title: str
    opened_at: Any
    closed_at: Optional[Any] = None
    created_by: Optional[str] = None

    def active_at(self, at) -> bool:
        return self.opened_at <= at and (self.closed_at is None or at < self.closed_at)

    def overlaps(self, start, end) -> bool:
        wo_end = self.closed_at
        if wo_end is None:
            return start >= self.opened_at or end > self.opened_at
        return start < wo_end and end > self.opened_at


@dataclass
class Mission:
    """任务载荷与计划窗口；forecast 为放行评估时刻的环境依据。"""

    id: str
    aircraft_id: str
    planned_start: Any
    planned_end: Any
    payload: list[dict] = field(default_factory=list)  # [{"type": ..., "kg": ...}]
    forecast: dict = field(default_factory=dict)       # {"windKt":..,"tempC":..,"precip":bool}
    status: str = "planned"  # planned / released / in_flight / completed / cancelled / intercepted
    created_by: Optional[str] = None


@dataclass
class FlightLog:
    """飞行日志（可迟到）：occurred 时刻为寿命归属时间，received_at 为进入系统时间。"""

    id: str
    aircraft_id: str
    off_block_at: Any
    on_block_at: Any
    cycles: float
    hours: float
    received_at: Any
    mission_id: Optional[str] = None
    payload: list[dict] = field(default_factory=list)
    weather: dict = field(default_factory=dict)
    # 受理时刻固化的配置与寿命快照，之后不再随当前装配变化
    config_snapshot: Optional[dict] = None
    late: bool = False


@dataclass
class EmergencyRelease:
    """紧急放行：只能由独立角色（监管人员）批准，带明确失效条件。

    失效条件（任一满足即失效）：
    - 评估时刻到达 valid_until；
    - 已用于放行的飞行次数达到 max_flights；
    - 被用于非指定任务；
    - 原始阻断项已不在 permitted_blockers 允许范围内。
    """

    id: str
    aircraft_id: str
    mission_id: str
    approved_by: str
    approver_role: str
    created_at: Any
    valid_until: Any
    max_flights: int
    permitted_blockers: list[str]
    reason: str = ""
    used_flight_ids: list[str] = field(default_factory=list)
    revoked: bool = False

    def valid_for(self, mission_id: str, at, blocker_codes: set[str]) -> Optional[str]:
        """返回失效原因；None 表示仍有效。"""
        if self.revoked:
            return "紧急放行已撤销"
        if self.mission_id != mission_id:
            return "紧急放行仅限指定任务"
        if at >= self.valid_until:
            return "紧急放行已到失效时刻"
        if len(self.used_flight_ids) >= self.max_flights:
            return "紧急放行可用飞行次数已用尽"
        unpermitted = blocker_codes - set(self.permitted_blockers)
        if unpermitted:
            return f"存在紧急放行未覆盖的阻断项: {sorted(unpermitted)}"
        return None

    def to_dict(self) -> dict:
        return asdict(self) | {
            "createdAt": timeutil.format_value(self.created_at),
            "validUntil": timeutil.format_value(self.valid_until),
        }


@dataclass
class ReleaseEvaluation:
    """一次放行评估：结论、阻断项及机务/调度各自需要的依据。"""

    id: str
    mission_id: str
    aircraft_id: str
    effective_at: Any
    evaluated_at: Any
    actor_id: str
    config: dict
    blockers: list[dict]
    life_basis: list[dict]
    signature_basis: list[dict]
    airframe_life: dict
    emergency_release_id: Optional[str] = None
    decision: str = "NO_GO"  # GO / NO_GO
    basis_revised_after: bool = False  # 被迟到日志追溯影响标记

    def blocker_codes(self) -> set[str]:
        return {b["code"] for b in self.blockers}
