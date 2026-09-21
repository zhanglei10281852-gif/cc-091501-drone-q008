"""服务层错误类型，携带 HTTP 状态码与结构化详情。"""


class ServiceError(Exception):
    status = 400
    code = "service_error"

    def __init__(self, message, *, detail=None):
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def to_dict(self):
        return {"error": self.code, "message": self.message, "detail": self.detail}


class ValidationError(ServiceError):
    status = 400
    code = "validation_error"


class ForbiddenError(ServiceError):
    status = 403
    code = "forbidden"


class NotFoundError(ServiceError):
    status = 404
    code = "not_found"


class ConflictError(ServiceError):
    status = 409
    code = "conflict"


class ReleaseBlocked(ConflictError):
    """任务不满足放行条件被拦截，携带完整的放行决策供按角色投影。"""

    code = "release_blocked"

    def __init__(self, decision):
        super().__init__("任务不满足放行条件，已拦截")
        self.decision = decision
