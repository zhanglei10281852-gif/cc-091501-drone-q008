"""HTTP 适配层：健康检查 + 适航管理 REST 接口。

岗位隔离在领域服务中强制：每个接口要求调用方（X-Actor-Id 头指定的人员）
具备相应角色，机务视图与调度视图返回不同字段。
"""

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from airworthiness import (
    AirworthinessService,
    AuthorizationError,
    ConflictError,
    NotFoundError,
    Store,
    ValidationError,
)

SERVICE_NAME = "drone-airworthiness-service"


def build_service() -> AirworthinessService:
    data_file = os.environ.get("AIRWORTHINESS_DATA")
    return AirworthinessService(Store(data_file))


class Handler(BaseHTTPRequestHandler):
    service: AirworthinessService = None  # 在 create_server 时注入

    # ------------------------------------------------------------ 基础
    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValidationError(f"请求体不是合法 JSON: {exc}")
        if not isinstance(data, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return data

    def _actor(self, body: dict) -> str:
        actor = self.headers.get("X-Actor-Id") or body.pop("actorId", None)
        if not actor:
            raise AuthorizationError("缺少 X-Actor-Id 请求头，无法确认操作岗位")
        return actor

    def _handle_error(self, exc: Exception) -> None:
        status = {
            ValidationError: 400,
            NotFoundError: 404,
            ConflictError: 409,
            AuthorizationError: 403,
        }.get(type(exc), 500)
        if status == 500:
            raise exc
        if isinstance(exc, (ValidationError, NotFoundError, ConflictError, AuthorizationError)):
            self._send_json(status, exc.to_dict())
        else:  # pragma: no cover - 防御
            self._send_json(status, {"error": "error", "message": str(exc)})

    # ------------------------------------------------------------ 路由
    def do_GET(self):  # noqa: N802
        try:
            parsed = urlparse(self.path)
            path, query = parsed.path, parse_qs(parsed.query)
            if path == "/health":
                self._send_json(200, {"status": "ok", "service": SERVICE_NAME})
                return
            m = re.fullmatch(r"/aircraft/([^/]+)/effective-config", path)
            if m:
                actor = self._actor({})
                # 有效配置含寿命读数，仅机务/监管可取；调度请用 dispatch 视图
                svc.authorize(actor, {"机务人员", "监管人员"})
                at = query.get("at", [None])[0]
                if not at:
                    raise ValidationError("缺少 at 查询参数")
                self._send_json(200, self.service.effective_config(m.group(1), at))
                return
            m = re.fullmatch(r"/aircraft/([^/]+)/views/mechanic", path)
            if m:
                actor = self._actor({})
                at = query.get("at", [None])[0]
                if not at:
                    raise ValidationError("缺少 at 查询参数")
                self._send_json(200, self.service.mechanic_view(m.group(1), at, actor))
                return
            m = re.fullmatch(r"/aircraft/([^/]+)/views/readonly", path)
            if m:
                actor = self._actor({})
                self._send_json(200, self.service.readonly_status(m.group(1), actor))
                return
            m = re.fullmatch(r"/missions/([^/]+)/views/dispatch", path)
            if m:
                actor = self._actor({})
                self._send_json(200, self.service.dispatch_view(m.group(1), actor))
                return
            if path == "/retro-impact":
                actor = self._actor({})
                since = query.get("since", [None])[0]
                self._send_json(200, self.service.retro_impact(actor, since))
                return
            self._send_json(404, {"error": "not_found"})
        except Exception as exc:  # noqa: BLE001
            self._handle_error(exc)

    def do_POST(self):  # noqa: N802
        try:
            path = urlparse(self.path).path
            body = self._read_json()
            svc = self.service

            # 主数据登记：系统配置类接口，不绑定操作岗位
            if path == "/people":
                self._send_json(201, svc.register_person(body))
                return
            if path == "/aircraft":
                self._send_json(201, svc.register_aircraft(body))
                return
            if path == "/components":
                self._send_json(201, svc.register_component(body))
                return
            if path == "/firmware":
                self._send_json(201, svc.register_firmware(body))
                return

            # 业务操作：必须确认操作岗位
            actor = self._actor(body)

            m = re.fullmatch(r"/people/([^/]+)/certifications", path)
            if m:
                self._send_json(201, svc.add_certification(m.group(1), body, actor))
                return
            m = re.fullmatch(r"/aircraft/([^/]+)/installs", path)
            if m:
                self._send_json(201, svc.install_component(
                    m.group(1), body["position"], body["serial"], body["at"], actor,
                    baseline_cycles=body.get("baselineCycles"),
                    baseline_hours=body.get("baselineHours"),
                ))
                return
            m = re.fullmatch(r"/aircraft/([^/]+)/removals", path)
            if m:
                self._send_json(200, svc.remove_component(
                    m.group(1), body["position"], body["at"], actor))
                return
            m = re.fullmatch(r"/aircraft/([^/]+)/firmware-flashes", path)
            if m:
                self._send_json(201, svc.flash_firmware(
                    m.group(1), body["firmwareId"], body["at"], actor))
                return
            m = re.fullmatch(r"/aircraft/([^/]+)/inspections", path)
            if m:
                body.setdefault("aircraftId", m.group(1))
                self._send_json(201, svc.record_inspection(body, actor))
                return
            if path == "/work-orders":
                self._send_json(201, svc.open_work_order(body, actor))
                return
            m = re.fullmatch(r"/work-orders/([^/]+)/close", path)
            if m:
                self._send_json(200, svc.close_work_order(m.group(1), body["at"], actor))
                return
            if path == "/missions":
                self._send_json(201, svc.create_mission(body, actor))
                return
            m = re.fullmatch(r"/missions/([^/]+)/release-evaluations", path)
            if m:
                self._send_json(201, svc.evaluate_release(m.group(1), actor))
                return
            m = re.fullmatch(r"/missions/([^/]+)/emergency-releases", path)
            if m:
                self._send_json(201, svc.grant_emergency_release(m.group(1), body, actor))
                return
            m = re.fullmatch(r"/emergency-releases/([^/]+)/revoke", path)
            if m:
                self._send_json(200, svc.revoke_emergency_release(m.group(1), actor))
                return
            if path == "/flight-logs":
                self._send_json(201, svc.ingest_flight_log(body, actor, body.pop("receivedAt", None)))
                return
            self._send_json(404, {"error": "not_found"})
        except Exception as exc:  # noqa: BLE001
            self._handle_error(exc)

    def log_message(self, *_args):
        return


def create_server():
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "0.0.0.0")
    service = build_service()
    handler = type("BoundHandler", (Handler,), {"service": service})
    return ThreadingHTTPServer((host, port), handler)
