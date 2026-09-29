"""Customer copy must reflect the frozen plan without modifying write inputs."""
from copy import deepcopy

import pytest

from business_assistant.controlled_bitrix_write import SingleProductWriteRequest
from business_assistant.product_enrichment_bridge import format_write_plan_text


@pytest.mark.parametrize("action", ["create", "existing", None])
def test_brand_plan_and_uncertainty_are_readable_without_mutation(action):
    request = SingleProductWriteRequest(
        tenant_id="tenant-a", title="TCL 55C6K", sku="55C6K",
        retail_price="54500", brand="TCL",
        characteristics={"screen_diagonal_cm": "139"},
        detailed_description="Цвет BLack. Вес 12.4 кг.",
    )
    preview = {
        "status": "REQUIRES_APPROVAL" if action else "UNRESOLVED",
        "reason": "" if action else "brand_lookup_failed",
        "active_after_create": False,
        "brand_plan": (dict(action=action, name="TCL", code="tcl", iblock_id=12,
                            brand_id=123 if action == "existing" else None)
                       if action else None),
        "will_write": ["internal write contract"],
        "canonical_payload": {"properties": {"property100": 123}},
    }
    characteristics = {
        "screen_diagonal_cm": {"value": "139", "confidence": "probable",
                               "bitrix_property_id": 154, "bitrix_writable": True},
        "color": {"value": "BLack", "confidence": "probable"},
    }
    before = deepcopy((request, preview, characteristics))
    text = format_write_plan_text(write_request=request, write_preview=preview,
                                  characteristic_status=characteristics)
    assert (request, preview, characteristics) == before
    assert "internal write contract" not in text
    assert "property100" not in text and "IBLOCK" not in text
    assert "диагональ экрана: 139 см (предположительно — требуется проверка)" in text
    assert "ТРЕБУЕТ ПРОВЕРКИ:" in text and "цвет" in text
    assert request.detailed_description in text
    assert "не заполнят отдельные поля каталога" in text
    if action == "create":
        assert "будет создан после подтверждения, до создания товара" in text
        assert "не будет записан без" not in text
    elif action == "existing":
        assert "будет использован существующий бренд" in text
        assert "будет создан после" not in text
    else:
        assert "привязка не подтверждена" in text
        assert "(brand_lookup_failed)" in text
        assert "План подготовлен" not in text
    if action:
        assert "неактивный — товар не будет опубликован" in text
    assert "только после отдельного явного подтверждения" in text


def test_missing_activity_is_not_invented():
    request = SingleProductWriteRequest(tenant_id="t", title="X", sku="X", retail_price="1")
    text = format_write_plan_text(write_request=request,
                                  write_preview={"status": "REQUIRES_APPROVAL"})
    assert "Статус активности после создания: не указан в плане." in text
    assert "неактивный" not in text
