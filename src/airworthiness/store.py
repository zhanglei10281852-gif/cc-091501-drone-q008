"""线程安全的内存存储；可选 JSON 落盘（路径由运行时配置显式指定）。"""

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class Store:
    def __init__(self, data_file: Optional[str] = None):
        self.lock = threading.RLock()
        self.data_file = Path(data_file) if data_file else None
        self.people: dict[str, dict] = {}
        self.aircraft: dict[str, dict] = {}
        self.components: dict[str, dict] = {}
        self.firmware: dict[str, dict] = {}
        self.installs: list[dict] = []
        self.flashes: list[dict] = []
        self.inspections: list[dict] = []
        self.work_orders: list[dict] = []
        self.missions: dict[str, dict] = {}
        self.flight_logs: list[dict] = []
        self.emergency_releases: dict[str, dict] = {}
        self.evaluations: dict[str, dict] = {}
        self._counters: dict[str, int] = {}
        if self.data_file and self.data_file.exists():
            self._load()

    def next_id(self, prefix: str) -> str:
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        return f"{prefix}-{self._counters[prefix]:04d}"

    # -- 持久化（仅在显式配置 data_file 时使用，测试默认不落盘） -----------
    def save(self) -> None:
        if not self.data_file:
            return
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "people": self.people,
            "aircraft": self.aircraft,
            "components": self.components,
            "firmware": self.firmware,
            "installs": self.installs,
            "flashes": self.flashes,
            "inspections": self.inspections,
            "work_orders": self.work_orders,
            "missions": self.missions,
            "flight_logs": self.flight_logs,
            "emergency_releases": self.emergency_releases,
            "evaluations": self.evaluations,
            "counters": self._counters,
        }
        tmp = self.data_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.data_file)

    def _load(self) -> None:
        payload = json.loads(self.data_file.read_text(encoding="utf-8"))
        self.people = payload.get("people", {})
        self.aircraft = payload.get("aircraft", {})
        self.components = payload.get("components", {})
        self.firmware = payload.get("firmware", {})
        self.installs = payload.get("installs", [])
        self.flashes = payload.get("flashes", [])
        self.inspections = payload.get("inspections", [])
        self.work_orders = payload.get("work_orders", [])
        self.missions = payload.get("missions", {})
        self.flight_logs = payload.get("flight_logs", [])
        self.emergency_releases = payload.get("emergency_releases", {})
        self.evaluations = payload.get("evaluations", {})
        self._counters = payload.get("counters", {})


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
