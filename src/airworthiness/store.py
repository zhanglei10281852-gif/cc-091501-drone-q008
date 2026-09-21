"""线程安全的数据存储，可选 JSON 文件落盘。

所有变更必须在 store.atomic() 临界区内完成，保证并发下同一机体
不会出现两个有效配置。落盘位置由运行时配置（STORE_FILE）指定，
测试使用纯内存存储，不依赖主机隐藏状态。
"""

import json
import os
import threading
from contextlib import contextmanager

from . import models as m


class Store:
    def __init__(self, path=None):
        self._lock = threading.RLock()
        self._path = path
        self.airframes: dict[str, m.Airframe] = {}
        self.parts: dict[str, m.Part] = {}
        self.install_events: list[m.InstallEvent] = []
        self.firmware_updates: list[m.FirmwareUpdate] = []
        self.qualifications: list[m.Qualification] = []
        self.signatures: list[m.InspectionSignature] = []
        self.work_orders: dict[str, m.WorkOrder] = {}
        self.missions: dict[str, m.Mission] = {}
        self.flight_logs: dict[str, m.FlightLog] = {}
        self.snapshots: dict[str, m.ConfigSnapshot] = {}
        self.occupancies: list[m.Occupancy] = []
        self.emergency_releases: dict[str, m.EmergencyRelease] = {}
        self.life_adjustments: list[m.LifeAdjustment] = []
        self._counters = {"install": 0, "snapshot": 0, "occupancy": 0, "adjustment": 0, "signature": 0}
        if path and os.path.exists(path):
            self._load()

    @contextmanager
    def atomic(self):
        with self._lock:
            yield self

    def next_id(self, kind, prefix):
        self._counters[kind] = self._counters.get(kind, 0) + 1
        return f"{prefix}-{self._counters[kind]:06d}"

    def save(self):
        if not self._path:
            return
        payload = self._serialize()
        tmp_path = self._path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
        os.replace(tmp_path, self._path)

    def _serialize(self):
        return {
            "airframes": [item.to_dict() for item in self.airframes.values()],
            "parts": [item.to_dict() for item in self.parts.values()],
            "install_events": [item.to_dict() for item in self.install_events],
            "firmware_updates": [item.to_dict() for item in self.firmware_updates],
            "qualifications": [item.to_dict() for item in self.qualifications],
            "signatures": [item.to_dict() for item in self.signatures],
            "work_orders": [item.to_dict() for item in self.work_orders.values()],
            "missions": [item.to_dict() for item in self.missions.values()],
            "flight_logs": [item.to_dict() for item in self.flight_logs.values()],
            "snapshots": [item.to_dict() for item in self.snapshots.values()],
            "occupancies": [item.to_dict() for item in self.occupancies],
            "emergency_releases": [item.to_dict() for item in self.emergency_releases.values()],
            "life_adjustments": [item.to_dict() for item in self.life_adjustments],
            "counters": self._counters,
        }

    def _load(self):
        with open(self._path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.airframes = {d["id"]: m.Airframe.from_dict(d) for d in data.get("airframes", [])}
        self.parts = {d["serial"]: m.Part.from_dict(d) for d in data.get("parts", [])}
        self.install_events = [m.InstallEvent.from_dict(d) for d in data.get("install_events", [])]
        self.firmware_updates = [m.FirmwareUpdate.from_dict(d) for d in data.get("firmware_updates", [])]
        self.qualifications = [m.Qualification.from_dict(d) for d in data.get("qualifications", [])]
        self.signatures = [m.InspectionSignature.from_dict(d) for d in data.get("signatures", [])]
        self.work_orders = {d["id"]: m.WorkOrder.from_dict(d) for d in data.get("work_orders", [])}
        self.missions = {d["id"]: m.Mission.from_dict(d) for d in data.get("missions", [])}
        self.flight_logs = {d["id"]: m.FlightLog.from_dict(d) for d in data.get("flight_logs", [])}
        self.snapshots = {d["id"]: m.ConfigSnapshot.from_dict(d) for d in data.get("snapshots", [])}
        self.occupancies = [m.Occupancy.from_dict(d) for d in data.get("occupancies", [])]
        self.emergency_releases = {d["id"]: m.EmergencyRelease.from_dict(d) for d in data.get("emergency_releases", [])}
        self.life_adjustments = [m.LifeAdjustment.from_dict(d) for d in data.get("life_adjustments", [])]
        self._counters.update(data.get("counters", {}))
