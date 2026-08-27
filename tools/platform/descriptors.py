"""Tool Platform — descriptor factories for integration adapters."""

from __future__ import annotations

from autonomy.capabilities import (
    CAP_CODE_EXECUTE,
    CAP_CRM_READ,
    CAP_CRM_WRITE,
    CAP_EXTERNAL_READ,
    CAP_EXTERNAL_WRITE,
    CAP_FILESYSTEM_READ,
    CAP_FILESYSTEM_WRITE,
    CAP_MESSAGE_SEND,
    CAP_SITE_READ,
    CAP_SITE_WRITE,
)
from autonomy.models import ACTION_READ, ACTION_WRITE
from tools.adapters import schema_hash_for
from tools.models import (
    DEFAULT_SEARCH_TIMEOUT_SECONDS,
    RETRY_NONE,
    RETRY_TRANSIENT,
    RETRY_WORKFLOW,
    SIDE_EFFECT_CRITICAL,
    SIDE_EFFECT_NONE,
    SIDE_EFFECT_READ,
    SIDE_EFFECT_WRITE,
    TOOL_TRUST_INTERNAL_SAFE,
    TOOL_TRUST_READ_ONLY_EXTERNAL,
    TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
    ToolDescriptor,
)

# Tool IDs
TOOL_FS_READ = "filesystem.read"
TOOL_FS_WRITE = "filesystem.write"
TOOL_HTTP = "http.request"
TOOL_TERMINAL = "terminal.execute"
TOOL_BROWSER = "browser.navigate"
TOOL_DOC_PARSE = "document.parse"
TOOL_DOC_SEARCH = "document.search"
TOOL_DOC_DETECT = "document.detect"
TOOL_DOC_EXTRACT = "document.extract"
TOOL_DOC_OCR = "document.ocr"
TOOL_DOC_STRUCTURED = "document.structured_extract"
TOOL_DOC_COMPARE = "document.compare"
TOOL_DOC_GENERATE = "document.generate"
TOOL_DOC_CONVERT = "document.convert"
TOOL_DATA_PROFILE = "data.profile"
TOOL_DATA_NORMALIZE = "data.normalize"
TOOL_DATA_SEARCH = "data.search"
TOOL_DATA_MATCH = "data.match"
TOOL_DATA_COMPARE = "data.compare"
TOOL_DATA_RECONCILE = "data.reconcile"
TOOL_DATA_AGGREGATE = "data.aggregate"
TOOL_DATA_GENERATE_EXCEL = "data.generate_excel"
TOOL_MCP = "mcp.invoke"
TOOL_CMS = "cms.product"
TOOL_BITRIX = "bitrix.catalog"
TOOL_ASPRO = "aspro.catalog"
TOOL_TELEGRAM = "telegram.message"
TOOL_CRM = "crm.entity"
TOOL_ONEC = "onec.sync"
TOOL_MARKETPLACE = "marketplace.product"
TOOL_SQL = "sql.query"
TOOL_EMAIL = "email.message"
TOOL_CALENDAR = "calendar.event"


def _read_desc(
    *,
    tool_id: str,
    name: str,
    description: str,
    category: str,
    adapter_id: str,
    capabilities: tuple[str, ...],
    operations: tuple[str, ...],
    enabled: bool = False,
    timeout: float = 15.0,
    network: bool = False,
) -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=tool_id,
        name=name,
        description=description,
        version="1.0.0",
        trust_level=TOOL_TRUST_READ_ONLY_EXTERNAL if network else TOOL_TRUST_INTERNAL_SAFE,
        capabilities_required=capabilities,
        action_types_supported=(ACTION_READ,),
        operations=operations,
        read_only=True,
        reversible=True,
        idempotency_required=False,
        timeout_seconds=timeout,
        enabled=enabled,
        network_access=network,
        category=category,
        adapter_id=adapter_id,
        side_effect_level=SIDE_EFFECT_READ if network else SIDE_EFFECT_NONE,
        retry_policy=RETRY_TRANSIENT if network else RETRY_NONE,
        schema_hash=schema_hash_for(operations),
    )


def _write_desc(
    *,
    tool_id: str,
    name: str,
    description: str,
    category: str,
    adapter_id: str,
    capabilities: tuple[str, ...],
    operations: tuple[str, ...],
    enabled: bool = False,
    timeout: float = 30.0,
    network: bool = True,
    critical: bool = False,
) -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=tool_id,
        name=name,
        description=description,
        version="1.0.0",
        trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
        capabilities_required=capabilities,
        action_types_supported=(ACTION_WRITE,),
        operations=operations,
        read_only=False,
        reversible=not critical,
        idempotency_required=True,
        timeout_seconds=timeout,
        enabled=enabled,
        network_access=network,
        category=category,
        adapter_id=adapter_id,
        side_effect_level=SIDE_EFFECT_CRITICAL if critical else SIDE_EFFECT_WRITE,
        retry_policy=RETRY_WORKFLOW,
        schema_hash=schema_hash_for(operations, ("idempotency_key",)),
    )


def filesystem_read_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_FS_READ,
        name="Filesystem Read",
        description="List/read/metadata within allowlisted workspace roots",
        category="filesystem",
        adapter_id="filesystem",
        capabilities=(CAP_FILESYSTEM_READ,),
        operations=("list", "read", "metadata"),
        enabled=enabled,
        timeout=10.0,
    )


def filesystem_write_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return _write_desc(
        tool_id=TOOL_FS_WRITE,
        name="Filesystem Write",
        description="Create/update files within sandbox workspace only",
        category="filesystem",
        adapter_id="filesystem",
        capabilities=(CAP_FILESYSTEM_WRITE,),
        operations=("write", "mkdir"),
        enabled=enabled,
        network=False,
    )


def http_request_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_HTTP,
        name="HTTP Request",
        description="Allowlisted HTTP GET with SSRF controls",
        category="http",
        adapter_id="http",
        capabilities=(CAP_EXTERNAL_READ,),
        operations=("get",),
        enabled=enabled,
        timeout=DEFAULT_SEARCH_TIMEOUT_SECONDS,
        network=True,
    )


def terminal_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=TOOL_TERMINAL,
        name="Terminal Execute",
        description="Controlled command execution (disabled by default in production)",
        version="1.0.0",
        trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
        capabilities_required=(CAP_CODE_EXECUTE,),
        action_types_supported=(ACTION_WRITE,),
        operations=("execute",),
        read_only=False,
        reversible=False,
        idempotency_required=True,
        timeout_seconds=30.0,
        enabled=enabled,
        network_access=False,
        category="terminal",
        adapter_id="terminal",
        side_effect_level=SIDE_EFFECT_CRITICAL,
        retry_policy=RETRY_NONE,
        schema_hash=schema_hash_for(("execute",), ("command", "idempotency_key")),
    )


def browser_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_BROWSER,
        name="Browser Navigate",
        description="Browser automation foundation (navigate/fetch rendered page)",
        category="browser",
        adapter_id="browser",
        capabilities=(CAP_EXTERNAL_READ, CAP_SITE_READ),
        operations=("navigate", "fetch", "screenshot"),
        enabled=enabled,
        timeout=30.0,
        network=True,
    )


def document_parse_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_DOC_PARSE,
        name="Document Parse",
        description="Parse PDF/DOCX/XLSX/CSV via DocumentService",
        category="document",
        adapter_id="document",
        capabilities=(CAP_FILESYSTEM_READ,),
        operations=("parse", "extract", "list_chunks"),
        enabled=enabled,
        timeout=60.0,
    )


def document_search_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_DOC_SEARCH,
        name="Document Search",
        description="Search parsed document chunks",
        category="document",
        adapter_id="document",
        capabilities=(CAP_FILESYSTEM_READ,),
        operations=("search",),
        enabled=enabled,
        timeout=30.0,
    )


def document_detect_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_DOC_DETECT,
        name="Document Detect",
        description="Magic/MIME/extension document type detection",
        category="document",
        adapter_id="document",
        capabilities=(CAP_FILESYSTEM_READ,),
        operations=("detect",),
        enabled=enabled,
        timeout=15.0,
    )


def document_extract_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_DOC_EXTRACT,
        name="Document Extract",
        description="Extract document content / metadata",
        category="document",
        adapter_id="document",
        capabilities=(CAP_FILESYSTEM_READ,),
        operations=("extract", "parse", "list_chunks"),
        enabled=enabled,
        timeout=60.0,
    )


def document_ocr_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_DOC_OCR,
        name="Document OCR",
        description="OCR for scans/images via OCRProvider",
        category="document",
        adapter_id="document",
        capabilities=(CAP_FILESYSTEM_READ,),
        operations=("ocr",),
        enabled=enabled,
        timeout=90.0,
    )


def document_structured_extract_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_DOC_STRUCTURED,
        name="Document Structured Extract",
        description="Schema-driven structured extraction (contract/invoice/etc)",
        category="document",
        adapter_id="document",
        capabilities=(CAP_FILESYSTEM_READ,),
        operations=("structured_extract",),
        enabled=enabled,
        timeout=60.0,
    )


def document_compare_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_DOC_COMPARE,
        name="Document Compare",
        description="Compare structured documents / sections",
        category="document",
        adapter_id="document",
        capabilities=(CAP_FILESYSTEM_READ,),
        operations=("compare",),
        enabled=enabled,
        timeout=60.0,
    )


def document_generate_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_DOC_GENERATE,
        name="Document Generate",
        description="Generate DOCX/PDF/TXT from typed templates (in-memory)",
        category="document",
        adapter_id="document",
        capabilities=(CAP_FILESYSTEM_WRITE,),
        operations=("generate",),
        enabled=enabled,
        timeout=60.0,
    )


def document_convert_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_DOC_CONVERT,
        name="Document Convert",
        description="Safe built-in document conversion (no LibreOffice shell)",
        category="document",
        adapter_id="document",
        capabilities=(CAP_FILESYSTEM_WRITE,),
        operations=("convert",),
        enabled=enabled,
        timeout=60.0,
    )


def data_profile_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_DATA_PROFILE,
        name="Data Profile",
        description="Profile dataset schema, roles, and types",
        category="data",
        adapter_id="data_intel",
        capabilities=(CAP_FILESYSTEM_READ,),
        operations=("profile",),
        enabled=enabled,
        timeout=30.0,
    )


def data_normalize_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_DATA_NORMALIZE,
        name="Data Normalize",
        description="Normalize and clean a tenant-scoped dataset",
        category="data",
        adapter_id="data_intel",
        capabilities=(CAP_FILESYSTEM_WRITE,),
        operations=("normalize",),
        enabled=enabled,
        timeout=60.0,
    )


def data_search_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_DATA_SEARCH,
        name="Data Search",
        description="Search dataset rows by INN/company/SKU/EAN and filters",
        category="data",
        adapter_id="data_intel",
        capabilities=(CAP_FILESYSTEM_READ,),
        operations=("search",),
        enabled=enabled,
        timeout=30.0,
    )


def data_match_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_DATA_MATCH,
        name="Data Match",
        description="Match counterparties or products with conflict rules",
        category="data",
        adapter_id="data_intel",
        capabilities=(CAP_FILESYSTEM_READ,),
        operations=("match",),
        enabled=enabled,
    )


def data_compare_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_DATA_COMPARE,
        name="Data Compare",
        description="Compare supplier price lists",
        category="data",
        adapter_id="data_intel",
        capabilities=(CAP_FILESYSTEM_READ,),
        operations=("compare",),
        enabled=enabled,
        timeout=60.0,
    )


def data_reconcile_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_DATA_RECONCILE,
        name="Data Reconcile",
        description="Reconcile payments, stock, or VAT amounts",
        category="data",
        adapter_id="data_intel",
        capabilities=(CAP_FILESYSTEM_READ,),
        operations=("reconcile",),
        enabled=enabled,
        timeout=60.0,
    )


def data_aggregate_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_DATA_AGGREGATE,
        name="Data Aggregate",
        description="Grouped aggregations over dataset rows",
        category="data",
        adapter_id="data_intel",
        capabilities=(CAP_FILESYSTEM_READ,),
        operations=("aggregate",),
        enabled=enabled,
    )


def data_generate_excel_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_DATA_GENERATE_EXCEL,
        name="Data Generate Excel",
        description="Generate finished searchable XLSX workbook",
        category="data",
        adapter_id="data_intel",
        capabilities=(CAP_FILESYSTEM_WRITE,),
        operations=("generate_excel",),
        enabled=enabled,
        timeout=120.0,
    )


def mcp_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_MCP,
        name="MCP Invoke",
        description="Optional MCP tool bridge — Core works with MCP disabled",
        category="mcp",
        adapter_id="mcp",
        capabilities=(CAP_EXTERNAL_READ,),
        operations=("invoke",),
        enabled=enabled,
        network=True,
    )


def cms_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=TOOL_CMS,
        name="CMS Product",
        description="Generic CMS product/catalog operations",
        version="1.0.0",
        trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
        capabilities_required=(CAP_SITE_READ, CAP_SITE_WRITE),
        action_types_supported=(ACTION_READ, ACTION_WRITE),
        operations=(
            "product_read",
            "product_create",
            "product_update",
            "category_read",
            "price_update",
            "stock_update",
            "media_attach",
            "seo_update",
            "publish",
            "unpublish",
        ),
        read_only=False,
        reversible=True,
        idempotency_required=True,
        timeout_seconds=30.0,
        enabled=enabled,
        network_access=True,
        category="cms",
        adapter_id="cms",
        side_effect_level=SIDE_EFFECT_WRITE,
        retry_policy=RETRY_WORKFLOW,
        schema_hash=schema_hash_for(("product_read", "product_update")),
    )


def bitrix_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    d = cms_descriptor(enabled=enabled)
    return ToolDescriptor(
        tool_id=TOOL_BITRIX,
        name="Bitrix Catalog",
        description="Bitrix24/CMS HTTP adapter foundation",
        version=d.version,
        trust_level=d.trust_level,
        capabilities_required=d.capabilities_required,
        action_types_supported=d.action_types_supported,
        operations=d.operations + ("order_read",),
        read_only=False,
        reversible=d.reversible,
        idempotency_required=True,
        timeout_seconds=30.0,
        enabled=enabled,
        network_access=True,
        category="bitrix",
        adapter_id="bitrix",
        side_effect_level=SIDE_EFFECT_WRITE,
        retry_policy=RETRY_WORKFLOW,
        schema_hash=schema_hash_for(d.operations),
    )


def aspro_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=TOOL_ASPRO,
        name="Aspro Catalog",
        description="Thin Aspro specialization over Bitrix/CMS adapter",
        version="1.0.0",
        trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
        capabilities_required=(CAP_SITE_READ, CAP_SITE_WRITE),
        action_types_supported=(ACTION_READ, ACTION_WRITE),
        operations=bitrix_descriptor().operations,
        read_only=False,
        reversible=True,
        idempotency_required=True,
        timeout_seconds=30.0,
        enabled=enabled,
        network_access=True,
        category="aspro",
        adapter_id="aspro",
        side_effect_level=SIDE_EFFECT_WRITE,
        retry_policy=RETRY_WORKFLOW,
        metadata={"extends": "bitrix"},
        schema_hash=schema_hash_for(("product_read",)),
    )


def telegram_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=TOOL_TELEGRAM,
        name="Telegram Message",
        description="Telegram read/send foundation",
        version="1.0.0",
        trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
        capabilities_required=(CAP_MESSAGE_SEND, CAP_EXTERNAL_READ),
        action_types_supported=(ACTION_READ, ACTION_WRITE),
        operations=("read_updates", "send_message", "send_file"),
        read_only=False,
        reversible=False,
        idempotency_required=True,
        timeout_seconds=20.0,
        enabled=enabled,
        network_access=True,
        category="telegram",
        adapter_id="telegram",
        side_effect_level=SIDE_EFFECT_WRITE,
        retry_policy=RETRY_WORKFLOW,
        schema_hash=schema_hash_for(("send_message",)),
    )


def crm_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=TOOL_CRM,
        name="CRM Entity",
        description="Generic CRM search/read/write foundation",
        version="1.0.0",
        trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
        capabilities_required=(CAP_CRM_READ, CAP_CRM_WRITE),
        action_types_supported=(ACTION_READ, ACTION_WRITE),
        operations=(
            "search",
            "read",
            "create_lead",
            "update_deal",
            "create_contact",
            "add_note",
        ),
        read_only=False,
        reversible=True,
        idempotency_required=True,
        timeout_seconds=30.0,
        enabled=enabled,
        network_access=True,
        category="crm",
        adapter_id="crm",
        side_effect_level=SIDE_EFFECT_WRITE,
        retry_policy=RETRY_WORKFLOW,
        schema_hash=schema_hash_for(("search", "read")),
    )


def onec_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_ONEC,
        name="1C Sync",
        description="Generic 1C integration contract (catalog/prices/stock/orders)",
        category="onec",
        adapter_id="onec",
        capabilities=(CAP_EXTERNAL_READ,),
        operations=("catalog_sync", "prices", "stock", "orders", "counterparties"),
        enabled=enabled,
        timeout=60.0,
        network=True,
    )


def marketplace_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=TOOL_MARKETPLACE,
        name="Marketplace Product",
        description="Generic marketplace adapter (Ozon/WB/Yandex scaffold)",
        version="1.0.0",
        trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
        capabilities_required=(CAP_EXTERNAL_READ, CAP_EXTERNAL_WRITE),
        action_types_supported=(ACTION_READ, ACTION_WRITE),
        operations=("products", "prices", "stock", "orders", "fees", "status"),
        read_only=False,
        reversible=True,
        idempotency_required=True,
        timeout_seconds=45.0,
        enabled=enabled,
        network_access=True,
        category="marketplace",
        adapter_id="marketplace",
        side_effect_level=SIDE_EFFECT_WRITE,
        retry_policy=RETRY_WORKFLOW,
        schema_hash=schema_hash_for(("products", "orders")),
    )


def sql_query_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_SQL,
        name="SQL Query",
        description="Parameterized read-only SELECT against allowlisted datasource",
        category="sql",
        adapter_id="sql",
        capabilities=(CAP_EXTERNAL_READ,),
        operations=("select",),
        enabled=enabled,
        timeout=15.0,
    )


def email_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=TOOL_EMAIL,
        name="Email Message",
        description="Email search/read/draft/send foundation",
        version="1.0.0",
        trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
        capabilities_required=(CAP_EXTERNAL_READ, CAP_MESSAGE_SEND),
        action_types_supported=(ACTION_READ, ACTION_WRITE),
        operations=("search", "read", "draft", "send"),
        read_only=False,
        reversible=False,
        idempotency_required=True,
        timeout_seconds=30.0,
        enabled=enabled,
        network_access=True,
        category="email",
        adapter_id="email",
        side_effect_level=SIDE_EFFECT_WRITE,
        retry_policy=RETRY_WORKFLOW,
        schema_hash=schema_hash_for(("search", "send")),
    )


def calendar_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=TOOL_CALENDAR,
        name="Calendar Event",
        description="Calendar list/search/create/update/cancel foundation",
        version="1.0.0",
        trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
        capabilities_required=(CAP_EXTERNAL_READ, CAP_EXTERNAL_WRITE),
        action_types_supported=(ACTION_READ, ACTION_WRITE),
        operations=("list", "search", "create", "update", "cancel"),
        read_only=False,
        reversible=True,
        idempotency_required=True,
        timeout_seconds=20.0,
        enabled=enabled,
        network_access=True,
        category="calendar",
        adapter_id="calendar",
        side_effect_level=SIDE_EFFECT_WRITE,
        retry_policy=RETRY_WORKFLOW,
        schema_hash=schema_hash_for(("list", "create")),
    )
