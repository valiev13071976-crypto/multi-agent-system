"""Register Tool Platform adapters into ToolRegistry."""

from __future__ import annotations

import os
from pathlib import Path

from tools.integration import IntegrationCredentialStore
from tools.platform.contracts import (
    BrowserReadAdapter,
    BrowserWriteAdapter,
    CalendarContractAdapter,
    CmsBitrixContractAdapter,
    CrmContractAdapter,
    DatabaseContractAdapter,
    DocumentsOcrContractAdapter,
    EmailContractAdapter,
    ExcelContractAdapter,
    ExternalApiContractAdapter,
    ImageContractAdapter,
    McpAdapter,
    ScrapingContractAdapter,
    SeoAnalyticsContractAdapter,
    TelegramContractAdapter,
    WebSearchContractAdapter,
)
from tools.platform.descriptors import (
    aspro_descriptor,
    bitrix_descriptor,
    browser_descriptor,
    browser_read_descriptor,
    browser_write_descriptor,
    calendar_descriptor,
    cms_descriptor,
    commerce_order_read_descriptor,
    commerce_order_validate_descriptor,
    commerce_reconcile_descriptor,
    commerce_catalog_analyze_descriptor,
    commerce_product_import_descriptor,
    commerce_price_decide_descriptor,
    commerce_price_apply_descriptor,
    commerce_cms_create_descriptor,
    commerce_cms_update_descriptor,
    commerce_cms_archive_descriptor,
    commerce_cms_stock_update_descriptor,
    commerce_order_ingest_descriptor,
    content_analyze_performance_descriptor,
    content_generate_copy_descriptor,
    content_generate_media_descriptor,
    content_get_descriptor,
    content_optimize_descriptor,
    content_publication_plan_descriptor,
    content_research_descriptor,
    content_status_descriptor,
    content_strategy_descriptor,
    crm_descriptor,
    data_aggregate_descriptor,
    data_compare_descriptor,
    data_duplicates_descriptor,
    data_generate_excel_descriptor,
    data_ingest_descriptor,
    data_match_descriptor,
    data_merge_descriptor,
    data_normalize_descriptor,
    data_profile_descriptor,
    data_reconcile_descriptor,
    data_search_descriptor,
    database_read_descriptor,
    database_write_descriptor,
    document_compare_descriptor,
    document_convert_descriptor,
    document_detect_descriptor,
    document_extract_descriptor,
    document_generate_descriptor,
    document_ocr_descriptor,
    document_parse_descriptor,
    document_search_descriptor,
    document_structured_extract_descriptor,
    edo_prepare_descriptor,
    edo_status_descriptor,
    email_descriptor,
    excel_inspect_descriptor,
    excel_read_range_descriptor,
    excel_write_descriptor,
    external_api_request_descriptor,
    filesystem_read_descriptor,
    filesystem_write_descriptor,
    fiscal_status_descriptor,
    http_request_descriptor,
    image_edit_descriptor,
    image_generate_descriptor,
    inventory_read_descriptor,
    inventory_release_descriptor,
    inventory_reserve_descriptor,
    knowledge_delete_descriptor,
    knowledge_ingest_descriptor,
    knowledge_retrieve_descriptor,
    knowledge_status_descriptor,
    marketplace_descriptor,
    memory_propose_descriptor,
    memory_read_descriptor,
    memory_write_descriptor,
    marking_status_descriptor,
    marking_transfer_descriptor,
    mcp_descriptor,
    media_analyze_descriptor,
    media_delete_descriptor,
    media_find_similar_descriptor,
    media_generate_descriptor,
    media_get_descriptor,
    media_ingest_descriptor,
    media_link_product_descriptor,
    media_transform_descriptor,
    media_validate_set_descriptor,
    onec_descriptor,
    bank_statement_read_descriptor,
    bank_transactions_descriptor,
    payments_allocate_descriptor,
    payments_execute_refund_descriptor,
    payments_match_descriptor,
    payments_prepare_refund_descriptor,
    payments_read_descriptor,
    payments_reconcile_descriptor,
    payments_status_descriptor,
    scrape_extract_descriptor,
    scrape_fetch_descriptor,
    seo_analytics_read_descriptor,
    seo_analytics_ingest_descriptor,
    seo_keyword_cluster_descriptor,
    seo_keyword_opportunities_descriptor,
    seo_keyword_research_descriptor,
    seo_meta_apply_descriptor,
    seo_meta_generate_descriptor,
    seo_meta_inspect_descriptor,
    seo_metadata_write_descriptor,
    seo_optimization_decide_descriptor,
    seo_optimization_measure_descriptor,
    seo_optimization_plan_descriptor,
    seo_performance_audit_descriptor,
    seo_sc_ingest_descriptor,
    seo_search_console_read_descriptor,
    seo_technical_audit_descriptor,
    b2b_supplier_create_descriptor,
    b2b_supplier_get_descriptor,
    b2b_wholesale_ingest_descriptor,
    b2b_wholesale_list_descriptor,
    b2b_wholesale_compare_descriptor,
    b2b_wholesale_changes_descriptor,
    b2b_inquiry_create_descriptor,
    b2b_quote_create_descriptor,
    b2b_quote_get_descriptor,
    b2b_quote_send_descriptor,
    b2b_order_draft_descriptor,
    b2b_order_submit_descriptor,
    b2b_handoff_create_descriptor,
    b2b_assistant_use_descriptor,
    telegram_message_send_descriptor,
    telegram_conversation_get_descriptor,
    sql_query_descriptor,
    supplier_read_descriptor,
    telegram_descriptor,
    terminal_descriptor,
    web_search_descriptor,
)
from tools.platform.documents import DocumentToolAdapter
from tools.platform.filesystem import FilesystemAdapter
from tools.platform.http_adapter import HttpAdapter
from tools.platform.scaffold import (
    AsproAdapter,
    BitrixAdapter,
    CmsScaffoldAdapter,
    ScaffoldAdapter,
    TerminalScaffoldAdapter,
)
from tools.registry import ToolRegistry
from data_intel.tools import DataIntelToolAdapter
from content_intel.tools import ContentIntelToolAdapter
from knowledge.tools import KnowledgeToolAdapter
from product_media.tools import ProductMediaToolAdapter
from commerce.tools import CommerceToolAdapter
from commerce.product_platform.tools import ProductPlatformToolAdapter
from payments.tools import PaymentsToolAdapter
from seo_marketing.tools import SeoMarketingToolAdapter
from b2b_commerce.tools import B2BCommerceToolAdapter


def _workspace_roots(env: dict | None = None) -> tuple[str, ...]:
    source = env if env is not None else os.environ
    raw = (source.get("TOOL_FS_ALLOWED_ROOTS") or "").strip()
    if raw:
        return tuple(p.strip() for p in raw.split(";") if p.strip())
    return (str(Path.cwd()),)


def _allowed_http_hosts(env: dict | None = None) -> tuple[str, ...]:
    source = env if env is not None else os.environ
    raw = (source.get("TOOL_HTTP_ALLOWED_HOSTS") or "").strip()
    if raw:
        return tuple(h.strip().lower() for h in raw.split(",") if h.strip())
    return ()


def _csv_env(env: dict | None, key: str) -> tuple[str, ...]:
    source = env if env is not None else os.environ
    raw = (source.get(key) or "").strip()
    if not raw:
        return ()
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def register_platform_tools(
    registry: ToolRegistry,
    *,
    env: dict | None = None,
    document_service=None,
    document_intelligence=None,
    data_intelligence=None,
    knowledge_service=None,
    memory_service=None,
    content_intelligence=None,
    commerce_service=None,
    payments_service=None,
    credential_store: IntegrationCredentialStore | None = None,
    product_media_service=None,
    product_platform_service=None,
    seo_marketing_service=None,
    b2b_commerce_service=None,
) -> dict:
    """Register platform adapters. Returns adapter map for health wiring."""
    creds = credential_store or IntegrationCredentialStore()
    fs = FilesystemAdapter(allowed_roots=_workspace_roots(env))
    http = HttpAdapter(
        allowed_hosts=_allowed_http_hosts(env),
        credential_store=creds,
    )
    doc = DocumentToolAdapter(document_service, intelligence=document_intelligence)
    doc_enabled = document_service is not None or document_intelligence is not None
    data = DataIntelToolAdapter(data_intelligence)
    data_enabled = data_intelligence is not None
    knowledge = KnowledgeToolAdapter(knowledge_service, memory_service=memory_service)
    knowledge_enabled = knowledge_service is not None
    content = ContentIntelToolAdapter(content_intelligence)
    content_enabled = content_intelligence is not None
    media = ProductMediaToolAdapter(product_media_service) if product_media_service else None
    media_enabled = product_media_service is not None
    commerce = CommerceToolAdapter(commerce_service, enabled=commerce_service is not None)
    commerce_enabled = commerce_service is not None
    product_platform = ProductPlatformToolAdapter(product_platform_service, enabled=product_platform_service is not None)
    product_platform_enabled = product_platform_service is not None
    seo_marketing = SeoMarketingToolAdapter(seo_marketing_service, enabled=seo_marketing_service is not None)
    seo_enabled = seo_marketing_service is not None
    b2b_commerce = B2BCommerceToolAdapter(b2b_commerce_service, enabled=b2b_commerce_service is not None)
    b2b_enabled = b2b_commerce_service is not None
    payments = PaymentsToolAdapter(payments_service, enabled=payments_service is not None)
    payments_enabled = payments_service is not None
    terminal_enabled = (env or os.environ).get("TOOL_TERMINAL_ENABLED", "").lower() in {
        "1",
        "true",
        "yes",
    }
    terminal = TerminalScaffoldAdapter(enabled=terminal_enabled)
    mcp_enabled = (env or os.environ).get("TOOL_MCP_ENABLED", "").lower() in {
        "1",
        "true",
        "yes",
    }
    mcp = McpAdapter(
        enabled=mcp_enabled,
        allowed_servers=_csv_env(env, "TOOL_MCP_ALLOWED_SERVERS"),
        allowed_tools=_csv_env(env, "TOOL_MCP_ALLOWED_TOOLS"),
        server_trust={
            s: "trusted" for s in _csv_env(env, "TOOL_MCP_TRUSTED_SERVERS")
        },
    )
    bitrix_enabled = (env or os.environ).get("BITRIX_ENABLED", "").lower() in {
        "1",
        "true",
        "yes",
    }
    bitrix = BitrixAdapter(credential_store=creds, enabled=bitrix_enabled)
    aspro = AsproAdapter(bitrix=bitrix, enabled=bitrix_enabled)

    # Contract adapters — mostly disabled for production vendors; available for tests
    excel = ExcelContractAdapter(enabled=False)
    browser_read = BrowserReadAdapter(enabled=False)
    browser_write = BrowserWriteAdapter(enabled=False)
    docs_contract = DocumentsOcrContractAdapter(enabled=False)
    email = EmailContractAdapter(enabled=False)
    calendar = CalendarContractAdapter(enabled=False)
    telegram = TelegramContractAdapter(enabled=False)
    crm = CrmContractAdapter(enabled=False)
    cms = CmsBitrixContractAdapter(enabled=False, bitrix=bitrix)
    external_api = ExternalApiContractAdapter(
        enabled=False, allowed_hosts=_allowed_http_hosts(env)
    )
    database = DatabaseContractAdapter(enabled=False)
    image = ImageContractAdapter(enabled=False)
    scrape = ScrapingContractAdapter(enabled=False)
    seo = SeoAnalyticsContractAdapter(enabled=False)
    web_search = WebSearchContractAdapter(enabled=False)

    registrations = [
        (filesystem_read_descriptor(enabled=bool(fs._roots)), fs),
        (filesystem_write_descriptor(enabled=False), fs),
        (http_request_descriptor(enabled=bool(http._allowed_hosts)), http),
        (terminal_descriptor(enabled=terminal_enabled), terminal),
        (browser_descriptor(enabled=False), browser_read),
        (browser_read_descriptor(enabled=False), browser_read),
        (browser_write_descriptor(enabled=False), browser_write),
        (document_parse_descriptor(enabled=doc_enabled), doc),
        (document_search_descriptor(enabled=document_service is not None), doc),
        (document_detect_descriptor(enabled=doc_enabled), doc),
        (document_extract_descriptor(enabled=doc_enabled), doc),
        (document_ocr_descriptor(enabled=doc_enabled), doc),
        (document_structured_extract_descriptor(enabled=doc_enabled), doc),
        (document_compare_descriptor(enabled=doc_enabled), doc),
        (document_generate_descriptor(enabled=doc_enabled), doc),
        (document_convert_descriptor(enabled=doc_enabled), doc),
        (data_ingest_descriptor(enabled=data_enabled), data),
        (data_profile_descriptor(enabled=data_enabled), data),
        (data_normalize_descriptor(enabled=data_enabled), data),
        (data_search_descriptor(enabled=data_enabled), data),
        (data_match_descriptor(enabled=data_enabled), data),
        (data_compare_descriptor(enabled=data_enabled), data),
        (data_reconcile_descriptor(enabled=data_enabled), data),
        (data_aggregate_descriptor(enabled=data_enabled), data),
        (data_duplicates_descriptor(enabled=data_enabled), data),
        (data_merge_descriptor(enabled=data_enabled), data),
        (data_generate_excel_descriptor(enabled=data_enabled), data),
        (knowledge_ingest_descriptor(enabled=knowledge_enabled), knowledge),
        (knowledge_retrieve_descriptor(enabled=knowledge_enabled), knowledge),
        (knowledge_delete_descriptor(enabled=knowledge_enabled), knowledge),
        (knowledge_status_descriptor(enabled=knowledge_enabled), knowledge),
        (memory_write_descriptor(enabled=memory_service is not None), knowledge),
        (memory_read_descriptor(enabled=memory_service is not None), knowledge),
        (memory_propose_descriptor(enabled=memory_service is not None), knowledge),
        (content_research_descriptor(enabled=content_enabled), content),
        (content_strategy_descriptor(enabled=content_enabled), content),
        (content_generate_copy_descriptor(enabled=content_enabled), content),
        (content_generate_media_descriptor(enabled=content_enabled), content),
        (content_publication_plan_descriptor(enabled=content_enabled), content),
        (content_analyze_performance_descriptor(enabled=content_enabled), content),
        (content_optimize_descriptor(enabled=content_enabled), content),
        (content_get_descriptor(enabled=content_enabled), content),
        (content_status_descriptor(enabled=content_enabled), content),
        (media_ingest_descriptor(enabled=media_enabled), media),
        (media_get_descriptor(enabled=media_enabled), media),
        (media_analyze_descriptor(enabled=media_enabled), media),
        (media_generate_descriptor(enabled=media_enabled), media),
        (media_transform_descriptor(enabled=media_enabled), media),
        (media_delete_descriptor(enabled=media_enabled), media),
        (media_link_product_descriptor(enabled=media_enabled), media),
        (media_find_similar_descriptor(enabled=media_enabled), media),
        (media_validate_set_descriptor(enabled=media_enabled), media),
        (commerce_order_read_descriptor(enabled=commerce_enabled), commerce),
        (commerce_order_validate_descriptor(enabled=commerce_enabled), commerce),
        (inventory_read_descriptor(enabled=commerce_enabled), commerce),
        (inventory_reserve_descriptor(enabled=commerce_enabled), commerce),
        (inventory_release_descriptor(enabled=commerce_enabled), commerce),
        (supplier_read_descriptor(enabled=commerce_enabled), commerce),
        (edo_status_descriptor(enabled=commerce_enabled), commerce),
        (edo_prepare_descriptor(enabled=commerce_enabled), commerce),
        (marking_status_descriptor(enabled=commerce_enabled), commerce),
        (marking_transfer_descriptor(enabled=commerce_enabled), commerce),
        (fiscal_status_descriptor(enabled=commerce_enabled), commerce),
        (commerce_reconcile_descriptor(enabled=commerce_enabled), commerce),
        (commerce_catalog_analyze_descriptor(enabled=product_platform_enabled), product_platform),
        (commerce_product_import_descriptor(enabled=product_platform_enabled), product_platform),
        (commerce_price_decide_descriptor(enabled=product_platform_enabled), product_platform),
        (commerce_price_apply_descriptor(enabled=product_platform_enabled), product_platform),
        (commerce_cms_create_descriptor(enabled=product_platform_enabled), product_platform),
        (commerce_cms_update_descriptor(enabled=product_platform_enabled), product_platform),
        (commerce_cms_archive_descriptor(enabled=product_platform_enabled), product_platform),
        (commerce_cms_stock_update_descriptor(enabled=product_platform_enabled), product_platform),
        (commerce_order_ingest_descriptor(enabled=product_platform_enabled), product_platform),
        (payments_read_descriptor(enabled=payments_enabled), payments),
        (payments_status_descriptor(enabled=payments_enabled), payments),
        (payments_match_descriptor(enabled=payments_enabled), payments),
        (payments_reconcile_descriptor(enabled=payments_enabled), payments),
        (bank_transactions_descriptor(enabled=payments_enabled), payments),
        (bank_statement_read_descriptor(enabled=payments_enabled), payments),
        (payments_allocate_descriptor(enabled=payments_enabled), payments),
        (payments_prepare_refund_descriptor(enabled=payments_enabled), payments),
        (payments_execute_refund_descriptor(enabled=payments_enabled), payments),
        (mcp_descriptor(enabled=mcp_enabled), mcp),
        (cms_descriptor(enabled=False), cms if cms else CmsScaffoldAdapter(adapter_id="cms")),
        (bitrix_descriptor(enabled=bitrix_enabled), bitrix),
        (aspro_descriptor(enabled=bitrix_enabled), aspro),
        (telegram_descriptor(enabled=False), telegram),
        (crm_descriptor(enabled=False), crm),
        (onec_descriptor(enabled=False), ScaffoldAdapter(adapter_id="onec")),
        (marketplace_descriptor(enabled=False), ScaffoldAdapter(adapter_id="marketplace")),
        (sql_query_descriptor(enabled=False), database),
        (email_descriptor(enabled=False), email),
        (calendar_descriptor(enabled=False), calendar),
        # New integration platform foundations (disabled by default)
        (excel_inspect_descriptor(enabled=False), excel),
        (excel_read_range_descriptor(enabled=False), excel),
        (excel_write_descriptor(enabled=False), excel),
        (image_generate_descriptor(enabled=media_enabled), media if media_enabled else image),
        (image_edit_descriptor(enabled=media_enabled), media if media_enabled else image),
        (scrape_fetch_descriptor(enabled=False), scrape),
        (scrape_extract_descriptor(enabled=False), scrape),
        (seo_analytics_read_descriptor(enabled=seo_enabled), seo_marketing if seo_enabled else seo),
        (seo_search_console_read_descriptor(enabled=seo_enabled), seo_marketing if seo_enabled else seo),
        (seo_metadata_write_descriptor(enabled=seo_enabled), seo_marketing if seo_enabled else seo),
        (seo_keyword_research_descriptor(enabled=seo_enabled), seo_marketing if seo_enabled else seo),
        (seo_keyword_cluster_descriptor(enabled=seo_enabled), seo_marketing if seo_enabled else seo),
        (seo_keyword_opportunities_descriptor(enabled=seo_enabled), seo_marketing if seo_enabled else seo),
        (seo_meta_inspect_descriptor(enabled=seo_enabled), seo_marketing if seo_enabled else seo),
        (seo_meta_generate_descriptor(enabled=seo_enabled), seo_marketing if seo_enabled else seo),
        (seo_meta_apply_descriptor(enabled=seo_enabled), seo_marketing if seo_enabled else seo),
        (seo_technical_audit_descriptor(enabled=seo_enabled), seo_marketing if seo_enabled else seo),
        (seo_performance_audit_descriptor(enabled=seo_enabled), seo_marketing if seo_enabled else seo),
        (seo_analytics_ingest_descriptor(enabled=seo_enabled), seo_marketing if seo_enabled else seo),
        (seo_sc_ingest_descriptor(enabled=seo_enabled), seo_marketing if seo_enabled else seo),
        (seo_optimization_plan_descriptor(enabled=seo_enabled), seo_marketing if seo_enabled else seo),
        (seo_optimization_measure_descriptor(enabled=seo_enabled), seo_marketing if seo_enabled else seo),
        (seo_optimization_decide_descriptor(enabled=seo_enabled), seo_marketing if seo_enabled else seo),
        (b2b_supplier_create_descriptor(enabled=b2b_enabled), b2b_commerce if b2b_enabled else telegram),
        (b2b_supplier_get_descriptor(enabled=b2b_enabled), b2b_commerce if b2b_enabled else telegram),
        (b2b_wholesale_ingest_descriptor(enabled=b2b_enabled), b2b_commerce if b2b_enabled else telegram),
        (b2b_wholesale_list_descriptor(enabled=b2b_enabled), b2b_commerce if b2b_enabled else telegram),
        (b2b_wholesale_compare_descriptor(enabled=b2b_enabled), b2b_commerce if b2b_enabled else telegram),
        (b2b_wholesale_changes_descriptor(enabled=b2b_enabled), b2b_commerce if b2b_enabled else telegram),
        (b2b_inquiry_create_descriptor(enabled=b2b_enabled), b2b_commerce if b2b_enabled else telegram),
        (b2b_quote_create_descriptor(enabled=b2b_enabled), b2b_commerce if b2b_enabled else telegram),
        (b2b_quote_get_descriptor(enabled=b2b_enabled), b2b_commerce if b2b_enabled else telegram),
        (b2b_quote_send_descriptor(enabled=b2b_enabled), b2b_commerce if b2b_enabled else telegram),
        (b2b_order_draft_descriptor(enabled=b2b_enabled), b2b_commerce if b2b_enabled else telegram),
        (b2b_order_submit_descriptor(enabled=b2b_enabled), b2b_commerce if b2b_enabled else telegram),
        (b2b_handoff_create_descriptor(enabled=b2b_enabled), b2b_commerce if b2b_enabled else telegram),
        (b2b_assistant_use_descriptor(enabled=b2b_enabled), b2b_commerce if b2b_enabled else telegram),
        (telegram_message_send_descriptor(enabled=b2b_enabled), b2b_commerce if b2b_enabled else telegram),
        (telegram_conversation_get_descriptor(enabled=b2b_enabled), b2b_commerce if b2b_enabled else telegram),
        (external_api_request_descriptor(enabled=False), external_api),
        (web_search_descriptor(enabled=False), web_search),
        (database_read_descriptor(enabled=False), database),
        (database_write_descriptor(enabled=False), database),
    ]
    adapters: dict = {
        "excel": excel,
        "browser": browser_read,
        "browser_write": browser_write,
        "documents_contract": docs_contract,
        "email": email,
        "calendar": calendar,
        "telegram": telegram,
        "crm": crm,
        "cms": cms,
        "external_api": external_api,
        "database": database,
        "image": media if media_enabled else image,
        "product_media": media,
        "product_platform": product_platform,
        "seo_marketing": seo_marketing if seo_enabled else seo,
        "b2b_commerce": b2b_commerce if b2b_enabled else telegram,
        "scrape": scrape,
        "seo": seo,
        "web_search": web_search,
        "mcp": mcp,
    }
    for desc, adapter in registrations:
        registry.register(desc, adapter=adapter)
        adapters[desc.adapter_id] = adapter
    return {"adapters": adapters, "credential_store": creds}
