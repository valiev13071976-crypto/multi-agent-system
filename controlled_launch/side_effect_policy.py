"""Shadow side-effect firewall — mandatory deny for external writes."""

from __future__ import annotations

from controlled_launch.errors import SHADOW_SIDE_EFFECT_DENIED, ControlledLaunchError

MUTATING_SIDE_EFFECTS = frozenset(
    {
        "payment",
        "charge",
        "subscription",
        "email_send",
        "telegram_send",
        "crm_write",
        "bitrix_write",
        "onec_write",
        "order_write",
        "price_write",
        "stock_write",
        "cms_publish",
        "external_delete",
        "side_effect_mutate",
    }
)


class ShadowSideEffectPolicy:
    """Deny all real external writes on shadow path."""

    @staticmethod
    def authorize(*, mode: str, side_effect_type: str, candidate_target: bool, shadow_path: bool) -> None:
        if not shadow_path:
            return
        effect = str(side_effect_type or "").strip().lower()
        if effect in MUTATING_SIDE_EFFECTS or effect.endswith("_write") or effect.endswith("_mutate"):
            raise ControlledLaunchError(
                SHADOW_SIDE_EFFECT_DENIED,
                details={"side_effect_type": effect, "mode": mode},
            )

    @staticmethod
    def allow_read_only(*, mode: str, side_effect_type: str, shadow_path: bool) -> bool:
        if not shadow_path:
            return True
        effect = str(side_effect_type or "").strip().lower()
        if effect in MUTATING_SIDE_EFFECTS:
            return False
        return effect in {"read", "query", "lookup", "dry_run"} or effect.endswith("_read")


class SideEffectOwnershipPolicy:
    """Prevent duplicate control+candidate business writes."""

    @staticmethod
    def owner_for_decision(*, control_target: bool, candidate_target: bool, logical_operation_id: str) -> str:
        if candidate_target and not control_target:
            return f"candidate:{logical_operation_id}"
        if control_target and not candidate_target:
            return f"control:{logical_operation_id}"
        if candidate_target and control_target:
            raise ControlledLaunchError("duplicate_side_effect_owner", details={"operation": logical_operation_id})
        return f"none:{logical_operation_id}"
