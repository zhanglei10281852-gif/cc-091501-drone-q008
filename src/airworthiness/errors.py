"""领域错误类型。"""


class AirworthinessError(Exception):
    """所有适航领域错误的基类。"""

    code = "airworthiness_error"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code

    def to_dict(self) -> dict:
        return {"error": self.code, "message": str(self)}


class ValidationError(AirworthinessError):
    code = "invalid_request"


class NotFoundError(AirworthinessError):
    code = "not_found"


class ConflictError(AirworthinessError):
    """同一机体出现两个有效配置、占用冲突等。"""

    code = "conflict"


class AuthorizationError(AirworthinessError):
    code = "forbidden"
