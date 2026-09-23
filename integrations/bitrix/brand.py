"""Fail-closed wire contract for the installed, authenticated brand resolver."""

from integrations.bitrix.errors import BitrixValidationError


def validate_brand_plan(value) -> dict:
    if not isinstance(value, dict):
        raise BitrixValidationError("brand_plan_invalid")
    plan = {key: value.get(key) for key in ("name", "code", "iblock_id", "brand_id", "action")}
    for key in ("name", "code"):
        if (
            not isinstance(plan[key], str)
            or (not plan[key].strip() and not (key == "code" and plan["action"] == "existing"))
            or len(plan[key]) > 255
        ):
            raise BitrixValidationError("brand_plan_invalid")
    for key in ("iblock_id", "brand_id"):
        raw = plan[key]
        if key == "brand_id" and raw is None and plan["action"] == "create":
            continue
        if isinstance(raw, bool) or not str(raw).isascii() or not str(raw).isdigit() or int(raw) <= 0:
            raise BitrixValidationError("brand_plan_invalid")
        plan[key] = str(int(raw))
    if plan["action"] not in ("existing", "create") or (plan["action"] == "create" and plan["brand_id"] is not None):
        raise BitrixValidationError("brand_plan_invalid")
    return plan


def validate_brand_resolution(value, plan: dict) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("created"), bool):
        raise BitrixValidationError("brand_resolution_invalid")
    resolved = validate_brand_plan({**value, "action": "existing"})
    if any(resolved[k] != plan[k] for k in ("name", "code", "iblock_id")) or (
        plan["brand_id"] is not None and resolved["brand_id"] != plan["brand_id"]
    ):
        raise BitrixValidationError("brand_resolution_mismatch")
    return {**resolved, "created": value["created"]}
