"""Governed brand scenario with real activation/gateway and mocked external REST."""
import json
from unittest.mock import patch

import httpx
import pytest

from business_assistant.controlled_bitrix_write import (
    prepare_single_product_write, execute_single_product_write, build_approval_signature,
    STATUS_REQUIRES_APPROVAL, STATUS_WRITE_VERIFIED, STATUS_UNRESOLVED,
    STATUS_APPROVAL_PLAN_CHANGED, STATUS_WRITE_PARTIAL_FAILURE,
)
from integrations.production.http import BoundedHttpClient
from integrations.bitrix.brand import validate_brand_plan
from integrations.bitrix.errors import BitrixValidationError
from test_bitrix_live_product_create_write import (
    _RecordingTransport, _LiveEnv, _bridge_and_activation, _request, TARGET_TENANT,
)


class BrandTransport(_RecordingTransport):
    def __init__(self, *, existing=False, fail_preview=False, lost_response=False, bad_readback=False, **kw):
        super().__init__(**kw)
        self.brand_id = '601' if existing else None
        self.brand_adds = 0
        self.fail_preview = fail_preview
        self.lost_response = lost_response
        self.bad_readback = bad_readback

    def __call__(self, method, url, **kwargs):
        rest = url.rsplit('/', 1)[-1].removesuffix('.json')
        body = kwargs.get('json_body') or {}
        if not rest.startswith('panda.brand.'):
            return super().__call__(method, url, **kwargs)
        self.calls.append((rest, json.loads(json.dumps(body))))
        if self.fail_preview:
            return httpx.Response(200, json={'error': 'PANDA_BRAND_AMBIGUOUS'})
        created = False
        if rest == 'panda.brand.resolve' and self.brand_id is None:
            self.brand_id = '601'
            self.brand_adds += 1
            created = True
            if self.lost_response:
                self.lost_response = False
                raise TimeoutError('response lost after commit')
        result = dict(name=body['name'], code=body['name'].lower(), iblock_id='12',
                      brand_id=self.brand_id, action='existing' if self.brand_id else 'create', created=created)
        if rest == 'panda.brand.preview' and body.get('expected_id') and self.bad_readback:
            result['brand_id'] = '999'
        return httpx.Response(200, json={'result': result})


@pytest.mark.parametrize('name', ['TCL', 'Apple', 'Xiaomi', 'Hisense', 'LG', 'A brand not known to Panda'])
def test_missing_generic_brand_preview_approval_create_link_readback(name):
    transport = BrandTransport()
    request = _request(brand=name, brand_id='')
    with _LiveEnv(), patch.object(BoundedHttpClient, 'request', side_effect=transport):
        bridge, _ = _bridge_and_activation()
        preview = prepare_single_product_write(bridge, tenant_id=TARGET_TENANT, request=request)
        assert preview['status'] == STATUS_REQUIRES_APPROVAL
        assert f'будет создан бренд {name} в IBLOCK 12' in preview['will_write']
        assert transport.brand_adds == transport.product_add_count == 0
        denied = execute_single_product_write(bridge, tenant_id=TARGET_TENANT, request=request, approved=False)
        assert denied['mutated'] is False
        result = execute_single_product_write(bridge, tenant_id=TARGET_TENANT, request=request, approved=True,
                                              expected_approval_signature=build_approval_signature(preview))
    assert result['status'] == STATUS_WRITE_VERIFIED
    assert result['read_back']['observed']['brand_id'] == '601'
    assert transport.brand_adds == transport.product_add_count == 1
    methods = [method for method, _ in transport.calls]
    assert methods.index('panda.brand.resolve') < methods.index('catalog.product.add')
    fields = next(body['fields'] for method, body in transport.calls if method == 'catalog.product.add')
    assert fields['property100'] == 601
    assert fields['active'] == 'N'


def test_existing_brand_is_reused_not_added():
    transport = BrandTransport(existing=True)
    with _LiveEnv(), patch.object(BoundedHttpClient, 'request', side_effect=transport):
        bridge, _ = _bridge_and_activation()
        result = execute_single_product_write(bridge, tenant_id=TARGET_TENANT,
                                             request=_request(brand_id=''), approved=True)
    assert result['status'] == STATUS_WRITE_VERIFIED
    assert transport.brand_adds == 0


def test_simple_product_brand_article_and_gallery_use_confirmed_catalog_fields():
    transport = BrandTransport(sections=[{'id': 70, 'name': 'Телевизоры', 'code': 'televizory'}])
    pictures = ({'filename': 'gallery.jpg', 'base64': 'Z2FsbGVyeQ=='},)
    request = _request(brand_id='', has_variant_offer=False, gallery_pictures=pictures, subcategory='Телевизоры')
    with _LiveEnv(), patch.object(BoundedHttpClient, 'request', side_effect=transport):
        bridge, _ = _bridge_and_activation()
        preview = prepare_single_product_write(bridge, tenant_id=TARGET_TENANT, request=request)
        assert preview['status'] == STATUS_REQUIRES_APPROVAL
        assert transport.brand_adds == transport.product_add_count == 0
        result = execute_single_product_write(
            bridge, tenant_id=TARGET_TENANT, request=request, approved=True,
            expected_approval_signature=build_approval_signature(preview),
        )
    assert result['status'] == STATUS_WRITE_VERIFIED
    fields = next(body['fields'] for method, body in transport.calls if method == 'catalog.product.add')
    assert fields['property241'] == request.sku
    assert fields['property124'] == [{'value': {'fileData': ['gallery.jpg', 'Z2FsbGVyeQ==']}}]
    assert fields['property100'] == 601
    assert fields['active'] == 'N'
    assert 'property283' not in fields and 'property280' not in fields
    assert transport.offer_add_count == 0
    assert result['read_back']['observed']['brand_id'] == '601'


def test_ambiguous_or_unavailable_lookup_blocks_all_writes():
    transport = BrandTransport(fail_preview=True)
    with _LiveEnv(), patch.object(BoundedHttpClient, 'request', side_effect=transport):
        bridge, _ = _bridge_and_activation()
        result = execute_single_product_write(bridge, tenant_id=TARGET_TENANT,
                                             request=_request(brand_id=''), approved=True)
    assert result['status'] == STATUS_UNRESOLVED
    assert transport.brand_adds == transport.product_add_count == 0


def test_lost_brand_response_retry_reuses_brand_and_same_approval():
    transport = BrandTransport(lost_response=True)
    request = _request(brand_id='')
    with _LiveEnv(), patch.object(BoundedHttpClient, 'request', side_effect=transport):
        bridge, _ = _bridge_and_activation()
        preview = prepare_single_product_write(bridge, tenant_id=TARGET_TENANT, request=request)
        signature = build_approval_signature(preview)
        failed = execute_single_product_write(bridge, tenant_id=TARGET_TENANT, request=request, approved=True,
                                              expected_approval_signature=signature)
        assert failed['status'] == STATUS_WRITE_PARTIAL_FAILURE
        assert failed['mutation_outcome'] == 'unknown'
        assert transport.product_add_count == 0
        # Fresh integration service: no in-process cache can conceal a duplicate.
        bridge, _ = _bridge_and_activation()
        result = execute_single_product_write(bridge, tenant_id=TARGET_TENANT, request=request, approved=True,
                                              expected_approval_signature=signature)
    assert result['status'] == STATUS_WRITE_VERIFIED
    assert transport.brand_adds == transport.product_add_count == 1
    keys = [body['idempotency_key'] for method, body in transport.calls if method == 'panda.brand.resolve']
    assert len(set(keys)) == 1


def test_existing_id_change_requires_new_approval():
    transport = BrandTransport(existing=True)
    request = _request(brand_id='')
    with _LiveEnv(), patch.object(BoundedHttpClient, 'request', side_effect=transport):
        bridge, _ = _bridge_and_activation()
        preview = prepare_single_product_write(bridge, tenant_id=TARGET_TENANT, request=request)
        transport.brand_id = '777'
        result = execute_single_product_write(bridge, tenant_id=TARGET_TENANT, request=request, approved=True,
                                              expected_approval_signature=build_approval_signature(preview))
    assert result['status'] == STATUS_APPROVAL_PLAN_CHANGED
    assert transport.brand_adds == transport.product_add_count == 0


def test_brand_readback_mismatch_blocks_product():
    transport = BrandTransport(bad_readback=True)
    with _LiveEnv(), patch.object(BoundedHttpClient, 'request', side_effect=transport):
        bridge, _ = _bridge_and_activation()
        request = _request(brand_id='')
        preview = prepare_single_product_write(bridge, tenant_id=TARGET_TENANT, request=request)
        result = execute_single_product_write(bridge, tenant_id=TARGET_TENANT,
                                             request=request, approved=True,
                                             expected_approval_signature=build_approval_signature(preview))
    assert result['status'] == STATUS_WRITE_PARTIAL_FAILURE
    assert transport.brand_adds == 1
    assert transport.product_add_count == 0


def test_product_failure_preserves_created_brand_and_never_claims_success():
    transport = BrandTransport(product_error={'error': 'CREATE_FAILED'})
    with _LiveEnv(), patch.object(BoundedHttpClient, 'request', side_effect=transport):
        bridge, _ = _bridge_and_activation()
        request = _request(brand_id='')
        preview = prepare_single_product_write(bridge, tenant_id=TARGET_TENANT, request=request)
        result = execute_single_product_write(bridge, tenant_id=TARGET_TENANT,
                                             request=request, approved=True,
                                             expected_approval_signature=build_approval_signature(preview))
    assert result['status'] == STATUS_WRITE_PARTIAL_FAILURE
    assert result['mutated'] is True
    assert result['brand']['brand_id'] == '601'


@pytest.mark.parametrize('bad', [None, {}, {'name': 'X', 'code': 'x', 'iblock_id': True, 'brand_id': None, 'action': 'create'},
                               {'name': 'X', 'code': 'x', 'iblock_id': '12', 'brand_id': 'word', 'action': 'existing'}])
def test_invalid_server_result_fails_closed(bad):
    with pytest.raises(BitrixValidationError):
        validate_brand_plan(bad)


def test_existing_brand_without_code_valid():
    assert validate_brand_plan(dict(name='X', code='', iblock_id=12, brand_id=3, action='existing'))['brand_id'] == '3'


def test_known_product_schema_blocker_prevents_orphan_brand():
    from integrations.bitrix import schema
    transport = BrandTransport()
    with _LiveEnv(), patch.object(BoundedHttpClient, 'request', side_effect=transport), patch.object(schema, 'catalog_property', return_value=None):
        bridge, _ = _bridge_and_activation()
        result = execute_single_product_write(bridge, tenant_id=TARGET_TENANT,
                                             request=_request(brand_id='', has_variant_offer=False), approved=True)
    assert result['status'] == STATUS_UNRESOLVED
    assert result['reason'] == 'bitrix_article_property_not_verified'
    assert transport.brand_adds == transport.product_add_count == 0
    assert not transport.calls


def test_product_confirmation_without_shown_brand_creation_plan_only_returns_preview():
    transport = BrandTransport()
    with _LiveEnv(), patch.object(BoundedHttpClient, 'request', side_effect=transport):
        bridge, _ = _bridge_and_activation()
        result = execute_single_product_write(bridge, tenant_id=TARGET_TENANT,
                                             request=_request(brand_id=''), approved=True)
    assert result['status'] == STATUS_REQUIRES_APPROVAL
    assert result['reason'] == 'brand_creation_requires_preview_confirmation'
    assert transport.brand_adds == transport.product_add_count == 0


def test_partial_brand_failure_message_never_claims_product_created():
    from business_assistant.controlled_bitrix_write import format_bitrix_write_result_text
    text = format_bitrix_write_result_text({'status': STATUS_WRITE_PARTIAL_FAILURE, 'brand': {'brand_id': '601', 'created': True}})
    assert 'Бренд создан: ID 601' in text
    assert 'Создание товара не подтверждено' in text
    assert 'товар создан' not in text


def test_single_conversation_missing_brand_requires_then_reuses_shown_plan():
    import asyncio
    from unittest.mock import AsyncMock
    from business_assistant.action_continuation import ActiveTask, ActionDecision
    from business_assistant.conversation_gateway import ConversationRequest
    from business_assistant.product_enrichment_bridge import serialize_write_request
    from test_panda_bitrix_conversational_write_confirmation import _panda

    async def scenario():
        transport = BrandTransport()
        with _LiveEnv(), patch.object(BoundedHttpClient, 'request', side_effect=transport):
            bridge, _ = _bridge_and_activation()
            panda, _ = _panda(bitrix_bridge=bridge)
            product_request = _request(brand_id='')
            task = ActiveTask(task_id='brand-task', tenant_id=TARGET_TENANT, owner_id='u',
                              conversation_id='c', family='excel', tool_id='', operation='', goal='',
                              parameters={'bitrix_enrichment_write_request': serialize_write_request(product_request)})
            panda._action_store.put(task)
            action = ActionDecision(decision='', readiness='', continuation='', task=task,
                                    arguments={'product_fields': {'sku': product_request.sku}, 'retail_price': product_request.retail_price})
            with patch.object(panda, '_auto_prepare_site_ready_card_if_needed', new=AsyncMock()), patch.object(bridge, 'check_live_existence', return_value=[]):
                first = await panda._invoke_controlled_bitrix_write(
                    ConversationRequest(text='confirm', tenant_id=TARGET_TENANT, user_id='u', conversation_id='c', request_id='first'), action)
                assert first.metadata['mutated'] is False
                assert 'будет создан бренд' in first.text
                assert transport.brand_adds == transport.product_add_count == 0
                assert task.parameters['bitrix_brand_approval_signature']['brand_plan']['action'] == 'create'
                second = await panda._invoke_controlled_bitrix_write(
                    ConversationRequest(text='confirm', tenant_id=TARGET_TENANT, user_id='u', conversation_id='c', request_id='second'), action)
                assert second.metadata['bitrix_write_result']['status'] == STATUS_WRITE_VERIFIED
                assert transport.brand_adds == transport.product_add_count == 1
    asyncio.run(scenario())
