"""领域服务包：无人机适航配置、放行评估、履历与岗位视图。"""

from .errors import AirworthinessError, NotFoundError, ConflictError, AuthorizationError, ValidationError
from .models import (
    ROLES,
    ROLE_RELEASE,
    ROLE_MECHANIC,
    ROLE_REGULATOR,
    ROLE_READONLY,
    KIND_BATTERY,
    KIND_PROPELLER,
    KIND_MOTOR,
    KIND_FLIGHT_CONTROLLER,
    UNIT_CYCLE,
    UNIT_HOUR,
)
from .store import Store
from .service import AirworthinessService

__all__ = [
    "AirworthinessError",
    "NotFoundError",
    "ConflictError",
    "AuthorizationError",
    "ValidationError",
    "Store",
    "AirworthinessService",
    "ROLES",
    "ROLE_RELEASE",
    "ROLE_MECHANIC",
    "ROLE_REGULATOR",
    "ROLE_READONLY",
    "KIND_BATTERY",
    "KIND_PROPELLER",
    "KIND_MOTOR",
    "KIND_FLIGHT_CONTROLLER",
    "UNIT_CYCLE",
    "UNIT_HOUR",
]
