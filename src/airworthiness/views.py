"""按角色投影放行决策：每个岗位只取得职责所需信息。

机务/放行/适航/监管可见寿命与签名依据；调度与只读用户只得到
可执行结论与阻断项（不含人员身份与寿命明细）。
"""

from .models import ReleaseDecision
from .timeutil import iso

ROLE_LABELS = {
    "maintenance": "机务人员",
    "dispatcher": "调度员",
    "release_officer": "放行员",
    "airworthiness_engineer": "适航工程师",
    "regulator": "监管人员",
    "readonly": "只读用户",
}
KNOWN_ROLES = frozenset(ROLE_LABELS)

#: 可查看寿命明细与签名依据的角色。
FULL_DETAIL_ROLES = frozenset({"maintenance", "release_officer", "airworthiness_engineer", "regulator"})


def decision_to_dict(decision: ReleaseDecision) -> dict:
    return {
        "airframe_id": decision.airframe_id,
        "mission_id": decision.mission_id,
        "evaluated_at": iso(decision.evaluated_at),
        "status": decision.status,
        "via": decision.via,
        "config_id": decision.config_id,
        "blockers": [
            {"code": b.code, "message": b.message, "detail": b.detail} for b in decision.blockers
        ],
        "life_basis": decision.life_basis,
        "signature_basis": decision.signature_basis,
        "emergency": decision.emergency,
    }


def _public_emergency(emergency):
    if not emergency:
        return None
    if emergency.get("valid") is False:
        return {"valid": False, "reason": emergency.get("reason")}
    return {
        "id": emergency.get("id"),
        "expires_at": emergency.get("expires_at"),
        "conditions": emergency.get("conditions"),
    }


def project_decision(decision: ReleaseDecision, role: str) -> dict:
    if role in FULL_DETAIL_ROLES:
        return decision_to_dict(decision)
    return {
        "airframe_id": decision.airframe_id,
        "mission_id": decision.mission_id,
        "evaluated_at": iso(decision.evaluated_at),
        "status": decision.status,
        "via": decision.via,
        "config_id": decision.config_id,
        "blockers": [{"code": b.code, "message": b.message} for b in decision.blockers],
        "emergency": _public_emergency(decision.emergency),
    }
