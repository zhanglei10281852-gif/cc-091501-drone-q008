import json
import os
import re
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from airworthiness.errors import ForbiddenError, NotFoundError, ReleaseBlocked, ServiceError, ValidationError
from airworthiness.service import AirworthinessService
from airworthiness.store import Store
from airworthiness.timeutil import now_utc, parse_ts
from airworthiness.views import KNOWN_ROLES, project_decision

SERVICE_NAME = "drone-airworthiness-service"


def build_service(store_path=None):
    path = store_path if store_path is not None else os.environ.get("STORE_FILE")
    return AirworthinessService(Store(path))


class Context:
    def __init__(self, service, role, body, query, params):
        self.service = service
        self.role = role
        self.body = body
        self.query = query
        self.params = params


def _register_airframe(ctx):
    return 201, ctx.service.register_airframe(ctx.body, role=ctx.role)


def _register_part(ctx):
    return 201, ctx.service.register_part(ctx.body, role=ctx.role)


def _grant_qualification(ctx):
    return 201, ctx.service.grant_qualification(ctx.body, role=ctx.role)


def _install_part(ctx):
    return 201, ctx.service.install_part(ctx.params["airframe_id"], ctx.body, role=ctx.role)


def _remove_part(ctx):
    return 200, ctx.service.remove_part(ctx.params["airframe_id"], ctx.body, role=ctx.role)


def _activate_firmware(ctx):
    return 201, ctx.service.activate_firmware(ctx.params["airframe_id"], ctx.body, role=ctx.role)


def _sync_part_life(ctx):
    return 200, ctx.service.sync_part_life(ctx.params["serial"], ctx.body, role=ctx.role)


def _sign_inspection(ctx):
    return 201, ctx.service.sign_inspection(ctx.params["airframe_id"], ctx.body, role=ctx.role)


def _open_work_order(ctx):
    return 201, ctx.service.open_work_order(ctx.body, role=ctx.role)


def _close_work_order(ctx):
    return 200, ctx.service.close_work_order(ctx.params["work_order_id"], ctx.body, role=ctx.role)


def _create_mission(ctx):
    return 201, ctx.service.create_mission(ctx.body, role=ctx.role)


def _evaluate_mission(ctx):
    at = parse_ts(ctx.query["at"][0], "at") if "at" in ctx.query else None
    decision = ctx.service.evaluate_mission(ctx.params["mission_id"], at=at, role=ctx.role)
    return 200, project_decision(decision, ctx.role)


def _dispatch_mission(ctx):
    at = parse_ts(ctx.body["at"], "at") if ctx.body.get("at") else None
    decision = ctx.service.dispatch_mission(ctx.params["mission_id"], at=at, role=ctx.role)
    return 200, project_decision(decision, ctx.role)


def _ingest_flight_log(ctx):
    return 201, ctx.service.ingest_flight_log(ctx.body, role=ctx.role)


def _approve_emergency(ctx):
    return 201, ctx.service.approve_emergency_release(ctx.body, role=ctx.role)


def _effective_config(ctx):
    at = parse_ts(ctx.query["at"][0], "at") if "at" in ctx.query else now_utc()
    return 200, ctx.service.effective_config_view(ctx.params["airframe_id"], at, role=ctx.role)


def _part_history(ctx):
    return 200, ctx.service.part_history(ctx.params["airframe_id"], role=ctx.role)


def _compile(pattern):
    return re.compile(re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern))


ROUTES = [
    ("POST", _compile("/airframes"), _register_airframe),
    ("POST", _compile("/parts"), _register_part),
    ("POST", _compile("/qualifications"), _grant_qualification),
    ("POST", _compile("/airframes/{airframe_id}/install"), _install_part),
    ("POST", _compile("/airframes/{airframe_id}/remove"), _remove_part),
    ("POST", _compile("/airframes/{airframe_id}/firmware"), _activate_firmware),
    ("POST", _compile("/parts/{serial}/life-sync"), _sync_part_life),
    ("POST", _compile("/airframes/{airframe_id}/inspections"), _sign_inspection),
    ("POST", _compile("/work-orders"), _open_work_order),
    ("POST", _compile("/work-orders/{work_order_id}/close"), _close_work_order),
    ("POST", _compile("/missions"), _create_mission),
    ("GET", _compile("/missions/{mission_id}/release"), _evaluate_mission),
    ("POST", _compile("/missions/{mission_id}/dispatch"), _dispatch_mission),
    ("POST", _compile("/flight-logs"), _ingest_flight_log),
    ("POST", _compile("/emergency-releases"), _approve_emergency),
    ("GET", _compile("/airframes/{airframe_id}/config"), _effective_config),
    ("GET", _compile("/airframes/{airframe_id}/history"), _part_history),
]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self._handle("GET")

    def do_POST(self):  # noqa: N802
        self._handle("POST")

    def _handle(self, method):
        role = self.headers.get("X-Role", "")
        try:
            status, payload = self._dispatch(method, role)
        except ReleaseBlocked as exc:
            status = exc.status
            payload = {
                "error": exc.code,
                "message": exc.message,
                "decision": project_decision(exc.decision, role),
            }
        except ServiceError as exc:
            status, payload = exc.status, exc.to_dict()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            status, payload = 500, {"error": "internal_error", "message": "服务内部错误"}
        self._send(status, payload)

    def _dispatch(self, method, role):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if method == "GET" and path == "/health":
            return 200, {"status": "ok", "service": SERVICE_NAME}
        if role not in KNOWN_ROLES:
            raise ForbiddenError("缺少或未知的角色标识（X-Role）")
        query = parse_qs(parsed.query)
        body = self._read_json() if method == "POST" else {}
        for route_method, regex, handler in ROUTES:
            if route_method != method:
                continue
            match = regex.fullmatch(path)
            if match:
                ctx = Context(self.server.service, role, body, query, match.groupdict())
                return handler(ctx)
        raise NotFoundError("接口不存在")

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ValidationError("请求体不是有效的 JSON") from None
        if not isinstance(data, dict):
            raise ValidationError("请求体必须为 JSON 对象")
        return data

    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def create_server(service=None, port=None):
    if port is None:
        port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "0.0.0.0")
    server = ThreadingHTTPServer((host, port), Handler)
    server.service = service if service is not None else build_service()
    return server
