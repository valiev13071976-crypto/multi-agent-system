"""A prepared batch preview remains the owner of a read-only follow-up."""
import os
from unittest.mock import patch

import pytest

from business_assistant.conversation_gateway import ConversationRequest
from test_panda_tcl_end_to_end_batch_write_defect_closure import (
    CONV, OWNER, TENANT, _bitrix_bridge_and_store, _panda, _register_upload,
    _tcl_subset_two_new_no_title_bytes,
)

FOLLOWUP = (
    "Покажи обновлённый полный предпросмотр только для 55C6K и 65RM7L "
    "из загруженного Excel. Цены — 54 990 и 64 990 рублей соответственно. "
    "Ничего пока не записывай."
)


async def prepared():
    bridge, store = _bitrix_bridge_and_store()
    panda, artifacts = _panda(bridge)
    ref = await _register_upload(artifacts, content=_tcl_subset_two_new_no_title_bytes())
    first = await panda.respond(ConversationRequest(
        text="Подготовь для сайта только товары 55C6K и 65RM7L. Ничего не записывай.",
        tenant_id=TENANT, user_id=OWNER, conversation_id=CONV,
        request_id="initial", attachment_refs=(ref,),
    ))
    assert first.metadata["action_decision"] == "PREVIEW_BATCH_BITRIX_SUBSET"
    assert first.metadata["bitrix_batch_subset_preview"]["count"] == 2
    return panda, store


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", ["true", "false"])
async def test_preview_without_repeating_site_target_uses_prepared_batch(managed):
    with patch.dict(os.environ, {"PANDA_MANAGED_AGENT_ENABLED": managed}):
        panda, store = await prepared()
        before = len(store.catalog(TENANT))
        result = await panda.respond(ConversationRequest(
            text=FOLLOWUP, tenant_id=TENANT, user_id=OWNER,
            conversation_id=CONV, request_id="repeat",
        ))
        assert result.metadata.get("action_decision") == "PREVIEW_BATCH_BITRIX_SUBSET"
        assert result.metadata["bitrix_batch_subset_preview"]["selected_skus"] == ["55C6K", "65RM7L"]
        assert "Розничная цена: 54990 RUB" in result.text
        assert "Розничная цена: 64990 RUB" in result.text
        assert "Статус после создания: неактивный" in result.text
        assert result.metadata["mutated"] is False
        assert len(store.catalog(TENANT)) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["statistics", "confirmation", "other_dataset", "other_conversation"])
async def test_contextual_preview_does_not_capture_unrelated_operations(case):
    panda, _ = await prepared()
    task = panda._action_store.get(tenant_id=TENANT, owner_id=OWNER, conversation_id=CONV)
    text = FOLLOWUP
    conv = CONV
    if case == "statistics":
        text = "Покажи среднюю цену 55C6K и 65RM7L в Excel."
    elif case == "confirmation":
        text = "Подтверждаю создание в Bitrix товаров 55C6K и 65RM7L."
    elif case == "other_dataset":
        task.parameters["bitrix_batch_dataset_id"] = "stale-dataset"
        panda._action_store.put(task)
    else:
        conv = "another-conversation"
    result = await panda._maybe_preview_batch_bitrix_subset(ConversationRequest(
        text=text, tenant_id=TENANT, user_id=OWNER, conversation_id=conv, request_id=case,
    ))
    assert result is None
