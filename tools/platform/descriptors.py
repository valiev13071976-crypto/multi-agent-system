"""Tool Platform — descriptor factories for integration adapters."""

from __future__ import annotations

from autonomy.capabilities import (
    CAP_BROWSER_READ,
    CAP_BROWSER_WRITE,
    CAP_CALENDAR_READ,
    CAP_CALENDAR_WRITE,
    CAP_CODE_EXECUTE,
    CAP_CRM_READ,
    CAP_CRM_WRITE,
    CAP_DB_READ,
    CAP_DB_WRITE,
    CAP_EMAIL_READ,
    CAP_EMAIL_SEND,
    CAP_EXTERNAL_READ,
    CAP_EXTERNAL_WRITE,
    CAP_FILESYSTEM_READ,
    CAP_FILESYSTEM_WRITE,
    CAP_IMAGE_EDIT,
    CAP_IMAGE_GENERATE,
    CAP_MCP_INVOKE,
    CAP_MESSAGE_SEND,
    CAP_SCRAPE,
    CAP_SEO_READ,
    CAP_SEO_WRITE,
    CAP_SITE_READ,
    CAP_SITE_WRITE,
    CAP_TELEGRAM_READ,
    CAP_TELEGRAM_SEND,
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
    TOOL_TRUST_WRITE_EXTERNAL_IRREVERSIBLE,
    TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
    ToolDescriptor,
)

# Tool IDs
TOOL_FS_READ = "filesystem.read"
TOOL_FS_WRITE = "filesystem.write"
TOOL_HTTP = "http.request"
TOOL_TERMINAL = "terminal.execute"
TOOL_BROWSER = "browser.navigate"
TOOL_BROWSER_READ = "browser.read"
TOOL_BROWSER_WRITE = "browser.write"
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
TOOL_DATA_INGEST = "data.ingest"
TOOL_DATA_NORMALIZE = "data.normalize"
TOOL_DATA_SEARCH = "data.search"
TOOL_DATA_MATCH = "data.match"
TOOL_DATA_COMPARE = "data.compare"
TOOL_DATA_RECONCILE = "data.reconcile"
TOOL_DATA_AGGREGATE = "data.aggregate"
TOOL_DATA_DUPLICATES = "data.duplicates"
TOOL_DATA_MERGE = "data.merge"
TOOL_DATA_GENERATE_EXCEL = "data.generate_excel"
TOOL_KNOWLEDGE_INGEST = "knowledge.ingest"
TOOL_KNOWLEDGE_RETRIEVE = "knowledge.retrieve"
TOOL_KNOWLEDGE_DELETE = "knowledge.delete"
TOOL_KNOWLEDGE_STATUS = "knowledge.status"
TOOL_MEMORY_WRITE = "memory.write"
TOOL_MEMORY_READ = "memory.read"
TOOL_MEMORY_PROPOSE = "memory.propose"
TOOL_CONTENT_RESEARCH = "content.research"
TOOL_CONTENT_STRATEGY = "content.create_strategy"
TOOL_CONTENT_GENERATE_COPY = "content.generate_copy"
TOOL_CONTENT_GENERATE_MEDIA = "content.generate_media"
TOOL_CONTENT_PUBLICATION_PLAN = "content.create_publication_plan"
TOOL_CONTENT_ANALYZE_PERFORMANCE = "content.analyze_performance"
TOOL_CONTENT_OPTIMIZE = "content.optimize"
TOOL_CONTENT_GET = "content.get"
TOOL_CONTENT_STATUS = "content.status"
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
TOOL_EXCEL_INSPECT = "excel.inspect"
TOOL_EXCEL_READ_RANGE = "excel.read_range"
TOOL_EXCEL_WRITE = "excel.write"
TOOL_IMAGE_GENERATE = "image.generate"
TOOL_IMAGE_EDIT = "image.edit"
TOOL_MEDIA_INGEST = "media.ingest"
TOOL_MEDIA_GET = "media.get"
TOOL_MEDIA_ANALYZE = "media.analyze"
TOOL_MEDIA_GENERATE = "media.generate"
TOOL_MEDIA_TRANSFORM = "media.transform"
TOOL_MEDIA_DELETE = "media.delete"
TOOL_MEDIA_LINK_PRODUCT = "media.link_product"
TOOL_MEDIA_FIND_SIMILAR = "media.find_similar"
TOOL_MEDIA_VALIDATE_SET = "media.validate_set"
TOOL_SCRAPE_FETCH = "scrape.fetch"
TOOL_SCRAPE_EXTRACT = "scrape.extract"
TOOL_SEO_ANALYTICS_READ = "seo.analytics_read"
TOOL_SEO_SEARCH_CONSOLE_READ = "seo.search_console_read"
TOOL_SEO_METADATA_WRITE = "seo.metadata_write"
TOOL_SEO_KEYWORD_RESEARCH = "seo.keyword.research"
TOOL_SEO_KEYWORD_CLUSTER = "seo.keyword.cluster"
TOOL_SEO_KEYWORD_OPPORTUNITIES = "seo.keyword.opportunities"
TOOL_SEO_META_INSPECT = "seo.meta.inspect"
TOOL_SEO_META_GENERATE = "seo.meta.generate"
TOOL_SEO_META_VALIDATE = "seo.meta.validate"
TOOL_SEO_META_APPLY = "seo.meta.apply"
TOOL_SEO_TECHNICAL_AUDIT = "seo.technical.audit"
TOOL_SEO_PERFORMANCE_AUDIT = "seo.performance.audit"
TOOL_SEO_ANALYTICS_INGEST = "seo.analytics.ingest"
TOOL_SEO_SC_INGEST = "seo.search_console.ingest"
TOOL_SEO_OPTIMIZATION_PLAN = "seo.optimization.plan"
TOOL_SEO_OPTIMIZATION_MEASURE = "seo.optimization.measure"
TOOL_SEO_OPTIMIZATION_DECIDE = "seo.optimization.decide"
TOOL_B2B_SUPPLIER_CREATE = "b2b.supplier.create"
TOOL_B2B_SUPPLIER_GET = "b2b.supplier.get"
TOOL_B2B_WHOLESALE_INGEST = "b2b.wholesale.ingest"
TOOL_B2B_WHOLESALE_LIST = "b2b.wholesale.list"
TOOL_B2B_WHOLESALE_COMPARE = "b2b.wholesale.compare"
TOOL_B2B_WHOLESALE_CHANGES = "b2b.wholesale.changes"
TOOL_B2B_INQUIRY_CREATE = "b2b.inquiry.create"
TOOL_B2B_QUOTE_CREATE = "b2b.quote.create"
TOOL_B2B_QUOTE_GET = "b2b.quote.get"
TOOL_B2B_QUOTE_SEND = "b2b.quote.send"
TOOL_B2B_ORDER_DRAFT = "b2b.order.draft"
TOOL_B2B_ORDER_SUBMIT = "b2b.order.submit"
TOOL_B2B_HANDOFF_CREATE = "b2b.handoff.create"
TOOL_B2B_ASSISTANT_USE = "b2b.assistant.use"
TOOL_TELEGRAM_MESSAGE_SEND = "telegram.message.send"
TOOL_TELEGRAM_CONVERSATION_GET = "telegram.conversation.get"
TOOL_EXTERNAL_API = "external_api.request"
TOOL_WEB_SEARCH = "web_search.query"
TOOL_DB_READ = "database.read"
TOOL_DB_WRITE = "database.write"
TOOL_COMMERCE_ORDER_READ = "commerce.order.read"
TOOL_COMMERCE_ORDER_VALIDATE = "commerce.order.validate"
TOOL_INVENTORY_READ = "inventory.read"
TOOL_INVENTORY_RESERVE = "inventory.reserve"
TOOL_INVENTORY_RELEASE = "inventory.release"
TOOL_SUPPLIER_READ = "supplier.read"
TOOL_EDO_STATUS = "edo.status"
TOOL_EDO_PREPARE = "edo.prepare"
TOOL_MARKING_STATUS = "marking.status"
TOOL_MARKING_TRANSFER = "marking.transfer"
TOOL_FISCAL_STATUS = "fiscal.status"
TOOL_COMMERCE_RECONCILE = "commerce.reconcile"
TOOL_COMMERCE_CATALOG_ANALYZE = "commerce.catalog.analyze"
TOOL_COMMERCE_PRODUCT_IMPORT = "commerce.product.import"
TOOL_COMMERCE_PRICE_DECIDE = "commerce.price.decide"
TOOL_COMMERCE_PRICE_APPLY = "commerce.price.apply"
TOOL_COMMERCE_CMS_CREATE = "commerce.cms.product.create"
TOOL_COMMERCE_CMS_UPDATE = "commerce.cms.product.update"
TOOL_COMMERCE_CMS_ARCHIVE = "commerce.cms.product.archive"
TOOL_COMMERCE_CMS_STOCK_UPDATE = "commerce.cms.stock.update"
TOOL_COMMERCE_ORDER_INGEST = "commerce.order.ingest"
TOOL_PAYMENTS_READ = "payments.read"
TOOL_PAYMENTS_STATUS = "payments.status"
TOOL_PAYMENTS_MATCH = "payments.match"
TOOL_PAYMENTS_RECONCILE = "payments.reconcile"
TOOL_PAYMENTS_ALLOCATE = "payments.allocate"
TOOL_PAYMENTS_PREPARE_REFUND = "payments.prepare_refund"
TOOL_PAYMENTS_EXECUTE_REFUND = "payments.execute_refund"
TOOL_BANK_TRANSACTIONS = "bank.transactions"
TOOL_BANK_STATEMENT_READ = "bank.statement.read"


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
        capabilities=(CAP_BROWSER_READ, CAP_EXTERNAL_READ, CAP_SITE_READ),
        operations=("navigate", "fetch", "screenshot"),
        enabled=enabled,
        timeout=30.0,
        network=True,
    )


def browser_read_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_BROWSER_READ,
        name="Browser Read",
        description="Browser read-only navigate/fetch/screenshot",
        category="browser",
        adapter_id="browser",
        capabilities=(CAP_BROWSER_READ,),
        operations=("navigate", "fetch", "screenshot", "read"),
        enabled=enabled,
        timeout=30.0,
        network=True,
    )


def browser_write_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return _write_desc(
        tool_id=TOOL_BROWSER_WRITE,
        name="Browser Write",
        description="Browser mutating actions (click/type/submit)",
        category="browser",
        adapter_id="browser_write",
        capabilities=(CAP_BROWSER_WRITE,),
        operations=("click", "type", "submit", "write"),
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


def data_ingest_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_DATA_INGEST,
        name="Data Ingest",
        description="Ingest governed XLS/XLSX/CSV dataset",
        category="data",
        adapter_id="data_intel",
        capabilities=(CAP_FILESYSTEM_WRITE,),
        operations=("ingest",),
        enabled=enabled,
        timeout=120.0,
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


def data_merge_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_DATA_MERGE,
        name="Data Merge",
        description="Governed keyed merge/join of dataset rows",
        category="data",
        adapter_id="data_intel",
        capabilities=(CAP_FILESYSTEM_WRITE,),
        operations=("merge",),
        enabled=enabled,
        timeout=60.0,
    )


def data_duplicates_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_DATA_DUPLICATES,
        name="Data Duplicates",
        description="Detect duplicate rows/keys in tenant dataset",
        category="data",
        adapter_id="data_intel",
        capabilities=(CAP_FILESYSTEM_READ,),
        operations=("duplicates",),
        enabled=enabled,
        timeout=60.0,
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
        capabilities=(CAP_MCP_INVOKE, CAP_EXTERNAL_READ),
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


def _commerce_read_descriptor(tool_id: str, name: str, operations: tuple[str, ...], *, enabled: bool) -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=tool_id,
        name=name,
        description=f"Commerce Operations: {name}",
        version="1.0.0",
        trust_level=TOOL_TRUST_READ_ONLY_EXTERNAL,
        capabilities_required=(CAP_EXTERNAL_READ,),
        action_types_supported=(ACTION_READ,),
        operations=operations,
        read_only=True,
        reversible=True,
        idempotency_required=False,
        timeout_seconds=20.0,
        enabled=enabled,
        network_access=False,
        category="commerce",
        adapter_id="commerce",
        side_effect_level=SIDE_EFFECT_READ,
        retry_policy=RETRY_NONE,
        schema_hash=schema_hash_for(operations),
    )


def commerce_order_read_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return _commerce_read_descriptor(
        TOOL_COMMERCE_ORDER_READ, "Commerce Order Read", ("order_read",), enabled=enabled
    )


def commerce_order_validate_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=TOOL_COMMERCE_ORDER_VALIDATE,
        name="Commerce Order Validate",
        description="Validate canonical commerce order state",
        version="1.0.0",
        trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
        capabilities_required=(CAP_EXTERNAL_READ, CAP_EXTERNAL_WRITE),
        action_types_supported=(ACTION_READ, ACTION_WRITE),
        operations=("order_validate",),
        read_only=False,
        reversible=True,
        idempotency_required=True,
        timeout_seconds=20.0,
        enabled=enabled,
        network_access=False,
        category="commerce",
        adapter_id="commerce",
        side_effect_level=SIDE_EFFECT_WRITE,
        retry_policy=RETRY_WORKFLOW,
        schema_hash=schema_hash_for(("order_validate",)),
    )


def inventory_read_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return _commerce_read_descriptor(
        TOOL_INVENTORY_READ, "Inventory Read", ("inventory_read",), enabled=enabled
    )


def inventory_reserve_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=TOOL_INVENTORY_RESERVE,
        name="Inventory Reserve",
        description="Reserve stock via Source of Truth gateway",
        version="1.0.0",
        trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
        capabilities_required=(CAP_EXTERNAL_WRITE,),
        action_types_supported=(ACTION_WRITE,),
        operations=("inventory_reserve",),
        read_only=False,
        reversible=True,
        idempotency_required=True,
        timeout_seconds=30.0,
        enabled=enabled,
        network_access=True,
        category="commerce",
        adapter_id="commerce",
        side_effect_level=SIDE_EFFECT_CRITICAL,
        retry_policy=RETRY_WORKFLOW,
        schema_hash=schema_hash_for(("inventory_reserve",)),
    )


def inventory_release_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=TOOL_INVENTORY_RELEASE,
        name="Inventory Release",
        description="Release reservation via Source of Truth gateway",
        version="1.0.0",
        trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
        capabilities_required=(CAP_EXTERNAL_WRITE,),
        action_types_supported=(ACTION_WRITE,),
        operations=("inventory_release",),
        read_only=False,
        reversible=True,
        idempotency_required=True,
        timeout_seconds=30.0,
        enabled=enabled,
        network_access=True,
        category="commerce",
        adapter_id="commerce",
        side_effect_level=SIDE_EFFECT_WRITE,
        retry_policy=RETRY_WORKFLOW,
        schema_hash=schema_hash_for(("inventory_release",)),
    )


def supplier_read_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return _commerce_read_descriptor(
        TOOL_SUPPLIER_READ, "Supplier Read", ("supplier_read",), enabled=enabled
    )


def edo_status_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return _commerce_read_descriptor(TOOL_EDO_STATUS, "EDO Status", ("edo_status",), enabled=enabled)


def edo_prepare_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=TOOL_EDO_PREPARE,
        name="EDO Prepare",
        description="Prepare EDO document via domain gateway",
        version="1.0.0",
        trust_level=TOOL_TRUST_WRITE_EXTERNAL_IRREVERSIBLE,
        capabilities_required=(CAP_EXTERNAL_WRITE,),
        action_types_supported=(ACTION_WRITE,),
        operations=("edo_prepare",),
        read_only=False,
        reversible=False,
        idempotency_required=True,
        timeout_seconds=45.0,
        enabled=enabled,
        network_access=True,
        category="commerce",
        adapter_id="commerce",
        side_effect_level=SIDE_EFFECT_CRITICAL,
        retry_policy=RETRY_WORKFLOW,
        schema_hash=schema_hash_for(("edo_prepare",)),
    )


def marking_status_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return _commerce_read_descriptor(
        TOOL_MARKING_STATUS, "Marking Status", ("marking_status",), enabled=enabled
    )


def marking_transfer_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=TOOL_MARKING_TRANSFER,
        name="Marking Transfer",
        description="Transfer marking code ownership via MarkingGateway",
        version="1.0.0",
        trust_level=TOOL_TRUST_WRITE_EXTERNAL_IRREVERSIBLE,
        capabilities_required=(CAP_EXTERNAL_WRITE,),
        action_types_supported=(ACTION_WRITE,),
        operations=("marking_transfer",),
        read_only=False,
        reversible=False,
        idempotency_required=True,
        timeout_seconds=45.0,
        enabled=enabled,
        network_access=True,
        category="commerce",
        adapter_id="commerce",
        side_effect_level=SIDE_EFFECT_CRITICAL,
        retry_policy=RETRY_WORKFLOW,
        schema_hash=schema_hash_for(("marking_transfer",)),
    )


def fiscal_status_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return _commerce_read_descriptor(
        TOOL_FISCAL_STATUS, "Fiscal Status", ("fiscal_status",), enabled=enabled
    )


def commerce_reconcile_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return _commerce_read_descriptor(
        TOOL_COMMERCE_RECONCILE, "Commerce Reconcile", ("reconcile",), enabled=enabled
    )


def _payments_read_descriptor(
    tool_id: str, name: str, operations: tuple[str, ...], *, enabled: bool = False
) -> ToolDescriptor:
    return _read_desc(
        tool_id=tool_id,
        name=name,
        description=name,
        category="payments",
        adapter_id="payments",
        capabilities=(CAP_EXTERNAL_READ,),
        operations=operations,
        enabled=enabled,
        timeout=20.0,
        network=False,
    )


def payments_read_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return _payments_read_descriptor(
        TOOL_PAYMENTS_READ, "Payments Read", ("payments_read",), enabled=enabled
    )


def payments_status_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return _payments_read_descriptor(
        TOOL_PAYMENTS_STATUS, "Payments Status", ("payments_status",), enabled=enabled
    )


def payments_match_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return _payments_read_descriptor(
        TOOL_PAYMENTS_MATCH, "Payments Match", ("payments_match",), enabled=enabled
    )


def payments_reconcile_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return _payments_read_descriptor(
        TOOL_PAYMENTS_RECONCILE,
        "Payments Reconcile",
        ("payments_reconcile",),
        enabled=enabled,
    )


def bank_transactions_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return _payments_read_descriptor(
        TOOL_BANK_TRANSACTIONS,
        "Bank Transactions",
        ("bank_transactions",),
        enabled=enabled,
    )


def bank_statement_read_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return _payments_read_descriptor(
        TOOL_BANK_STATEMENT_READ,
        "Bank Statement Read",
        ("bank_statement_read",),
        enabled=enabled,
    )


def payments_allocate_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=TOOL_PAYMENTS_ALLOCATE,
        name="Payments Allocate",
        description="Allocate payment to order/invoice",
        version="1.0.0",
        trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
        capabilities_required=(CAP_EXTERNAL_WRITE,),
        action_types_supported=(ACTION_WRITE,),
        operations=("payments_allocate",),
        read_only=False,
        reversible=True,
        idempotency_required=True,
        timeout_seconds=30.0,
        enabled=enabled,
        network_access=False,
        category="payments",
        adapter_id="payments",
        side_effect_level=SIDE_EFFECT_WRITE,
        retry_policy=RETRY_WORKFLOW,
        schema_hash=schema_hash_for(("payments_allocate",), ("idempotency_key",)),
    )


def payments_prepare_refund_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=TOOL_PAYMENTS_PREPARE_REFUND,
        name="Payments Prepare Refund",
        description="Prepare refund request (does not execute)",
        version="1.0.0",
        trust_level=TOOL_TRUST_WRITE_EXTERNAL_REVERSIBLE,
        capabilities_required=(CAP_EXTERNAL_WRITE,),
        action_types_supported=(ACTION_WRITE,),
        operations=("payments_prepare_refund",),
        read_only=False,
        reversible=True,
        idempotency_required=True,
        timeout_seconds=30.0,
        enabled=enabled,
        network_access=True,
        category="payments",
        adapter_id="payments",
        side_effect_level=SIDE_EFFECT_WRITE,
        retry_policy=RETRY_WORKFLOW,
        schema_hash=schema_hash_for(("payments_prepare_refund",), ("idempotency_key",)),
    )


def payments_execute_refund_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=TOOL_PAYMENTS_EXECUTE_REFUND,
        name="Payments Execute Refund",
        description="Execute refund via PaymentGateway — HITL/capability protected",
        version="1.0.0",
        trust_level=TOOL_TRUST_WRITE_EXTERNAL_IRREVERSIBLE,
        capabilities_required=(CAP_EXTERNAL_WRITE,),
        action_types_supported=(ACTION_WRITE,),
        operations=("payments_execute_refund",),
        read_only=False,
        reversible=False,
        idempotency_required=True,
        timeout_seconds=45.0,
        enabled=enabled,
        network_access=True,
        category="payments",
        adapter_id="payments",
        side_effect_level=SIDE_EFFECT_CRITICAL,
        retry_policy=RETRY_WORKFLOW,
        schema_hash=schema_hash_for(("payments_execute_refund",), ("idempotency_key",)),
    )


# --- Integration platform foundation descriptors (4.x) ---


def excel_inspect_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_EXCEL_INSPECT,
        name="Excel Inspect",
        description="Inspect workbook sheets/structure (foundation only)",
        category="excel",
        adapter_id="excel",
        capabilities=(CAP_FILESYSTEM_READ,),
        operations=("inspect",),
        enabled=enabled,
        timeout=60.0,
    )


def excel_read_range_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_EXCEL_READ_RANGE,
        name="Excel Read Range",
        description="Read a cell range from a workbook (foundation only)",
        category="excel",
        adapter_id="excel",
        capabilities=(CAP_FILESYSTEM_READ,),
        operations=("read_range",),
        enabled=enabled,
        timeout=60.0,
    )


def excel_write_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return _write_desc(
        tool_id=TOOL_EXCEL_WRITE,
        name="Excel Write",
        description="Write cells to a workbook (foundation; SideEffect path)",
        category="excel",
        adapter_id="excel",
        capabilities=(CAP_FILESYSTEM_WRITE,),
        operations=("write",),
        enabled=enabled,
        network=False,
    )


def image_generate_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return _write_desc(
        tool_id=TOOL_IMAGE_GENERATE,
        name="Image Generate",
        description="Image generation foundation (scaffold)",
        category="image",
        adapter_id="image",
        capabilities=(CAP_IMAGE_GENERATE,),
        operations=("generate",),
        enabled=enabled,
    )


def image_edit_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return _write_desc(
        tool_id=TOOL_IMAGE_EDIT,
        name="Image Edit",
        description="Image edit foundation (scaffold)",
        category="image",
        adapter_id="image",
        capabilities=(CAP_IMAGE_EDIT,),
        operations=("edit",),
        enabled=enabled,
    )


def scrape_fetch_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_SCRAPE_FETCH,
        name="Scrape Fetch",
        description="Fetch page content for extraction (foundation)",
        category="scrape",
        adapter_id="scrape",
        capabilities=(CAP_SCRAPE, CAP_EXTERNAL_READ),
        operations=("fetch",),
        enabled=enabled,
        timeout=45.0,
        network=True,
    )


def scrape_extract_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_SCRAPE_EXTRACT,
        name="Scrape Extract",
        description="Extract structured fields from fetched content",
        category="scrape",
        adapter_id="scrape",
        capabilities=(CAP_SCRAPE,),
        operations=("extract",),
        enabled=enabled,
        timeout=45.0,
        network=True,
    )


def seo_analytics_read_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    from seo_marketing.capabilities import CAP_SEO_ANALYTICS_READ

    return _read_desc(
        tool_id=TOOL_SEO_ANALYTICS_READ,
        name="SEO Analytics Read",
        description="Read SEO analytics metrics",
        category="seo",
        adapter_id="seo_marketing",
        capabilities=(CAP_SEO_ANALYTICS_READ,),
        operations=("analytics_ingest", "analytics_query"),
        enabled=enabled,
        network=True,
    )


def seo_search_console_read_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    from seo_marketing.capabilities import CAP_SEO_SEARCH_CONSOLE_READ

    return _read_desc(
        tool_id=TOOL_SEO_SEARCH_CONSOLE_READ,
        name="SEO Search Console Read",
        description="Read Search Console data",
        category="seo",
        adapter_id="seo_marketing",
        capabilities=(CAP_SEO_SEARCH_CONSOLE_READ,),
        operations=("search_console_ingest", "search_console_query"),
        enabled=enabled,
        network=True,
    )


def seo_metadata_write_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    from seo_marketing.capabilities import CAP_SEO_META_APPLY

    return _write_desc(
        tool_id=TOOL_SEO_METADATA_WRITE,
        name="SEO Metadata Write",
        description="Apply SEO metadata (SideEffect path)",
        category="seo",
        adapter_id="seo_marketing",
        capabilities=(CAP_SEO_META_APPLY,),
        operations=("metadata_write", "meta_apply"),
        enabled=enabled,
    )


def seo_keyword_research_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    from seo_marketing.capabilities import CAP_SEO_KEYWORD_ANALYZE

    return _write_desc(
        tool_id=TOOL_SEO_KEYWORD_RESEARCH,
        name="SEO Keyword Research",
        description="Governed keyword research",
        category="seo",
        adapter_id="seo_marketing",
        capabilities=(CAP_SEO_KEYWORD_ANALYZE,),
        operations=("keyword_research", "research"),
        enabled=enabled,
    )


def seo_keyword_cluster_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    from seo_marketing.capabilities import CAP_SEO_KEYWORD_ANALYZE

    return _write_desc(
        tool_id=TOOL_SEO_KEYWORD_CLUSTER,
        name="SEO Keyword Cluster",
        description="Cluster keywords",
        category="seo",
        adapter_id="seo_marketing",
        capabilities=(CAP_SEO_KEYWORD_ANALYZE,),
        operations=("keyword_cluster", "cluster"),
        enabled=enabled,
    )


def seo_keyword_opportunities_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    from seo_marketing.capabilities import CAP_SEO_KEYWORD_ANALYZE

    return _read_desc(
        tool_id=TOOL_SEO_KEYWORD_OPPORTUNITIES,
        name="SEO Keyword Opportunities",
        description="Score keyword opportunities",
        category="seo",
        adapter_id="seo_marketing",
        capabilities=(CAP_SEO_KEYWORD_ANALYZE,),
        operations=("keyword_opportunities", "opportunities"),
        enabled=enabled,
    )


def seo_meta_inspect_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    from seo_marketing.capabilities import CAP_SEO_READ

    return _read_desc(
        tool_id=TOOL_SEO_META_INSPECT,
        name="SEO Meta Inspect",
        description="Inspect page metadata",
        category="seo",
        adapter_id="seo_marketing",
        capabilities=(CAP_SEO_READ,),
        operations=("meta_inspect", "inspect"),
        enabled=enabled,
    )


def seo_meta_generate_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    from seo_marketing.capabilities import CAP_SEO_META_GENERATE

    return _write_desc(
        tool_id=TOOL_SEO_META_GENERATE,
        name="SEO Meta Generate",
        description="Generate metadata recommendations",
        category="seo",
        adapter_id="seo_marketing",
        capabilities=(CAP_SEO_META_GENERATE,),
        operations=("meta_generate", "generate"),
        enabled=enabled,
    )


def seo_meta_apply_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    from seo_marketing.capabilities import CAP_SEO_META_APPLY

    return _write_desc(
        tool_id=TOOL_SEO_META_APPLY,
        name="SEO Meta Apply",
        description="Apply validated metadata recommendation",
        category="seo",
        adapter_id="seo_marketing",
        capabilities=(CAP_SEO_META_APPLY,),
        operations=("meta_apply", "apply"),
        enabled=enabled,
    )


def seo_technical_audit_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    from seo_marketing.capabilities import CAP_SEO_TECHNICAL_READ

    return _write_desc(
        tool_id=TOOL_SEO_TECHNICAL_AUDIT,
        name="SEO Technical Audit",
        description="Technical SEO audit over crawl snapshot",
        category="seo",
        adapter_id="seo_marketing",
        capabilities=(CAP_SEO_TECHNICAL_READ,),
        operations=("technical_audit", "audit"),
        enabled=enabled,
    )


def seo_performance_audit_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    from seo_marketing.capabilities import CAP_SEO_PERFORMANCE_READ

    return _write_desc(
        tool_id=TOOL_SEO_PERFORMANCE_AUDIT,
        name="SEO Performance Audit",
        description="Performance/speed audit",
        category="seo",
        adapter_id="seo_marketing",
        capabilities=(CAP_SEO_PERFORMANCE_READ,),
        operations=("performance_audit",),
        enabled=enabled,
    )


def seo_analytics_ingest_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    from seo_marketing.capabilities import CAP_SEO_ANALYTICS_READ

    return _write_desc(
        tool_id=TOOL_SEO_ANALYTICS_INGEST,
        name="SEO Analytics Ingest",
        description="Ingest analytics snapshot",
        category="seo",
        adapter_id="seo_marketing",
        capabilities=(CAP_SEO_ANALYTICS_READ,),
        operations=("analytics_ingest",),
        enabled=enabled,
        network=True,
    )


def seo_sc_ingest_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    from seo_marketing.capabilities import CAP_SEO_SEARCH_CONSOLE_READ

    return _write_desc(
        tool_id=TOOL_SEO_SC_INGEST,
        name="SEO Search Console Ingest",
        description="Ingest Search Console snapshot",
        category="seo",
        adapter_id="seo_marketing",
        capabilities=(CAP_SEO_SEARCH_CONSOLE_READ,),
        operations=("search_console_ingest",),
        enabled=enabled,
        network=True,
    )


def seo_optimization_plan_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    from seo_marketing.capabilities import CAP_SEO_OPTIMIZATION_PLAN

    return _write_desc(
        tool_id=TOOL_SEO_OPTIMIZATION_PLAN,
        name="SEO Optimization Plan",
        description="Create optimization plan",
        category="seo",
        adapter_id="seo_marketing",
        capabilities=(CAP_SEO_OPTIMIZATION_PLAN,),
        operations=("optimization_plan",),
        enabled=enabled,
    )


def seo_optimization_measure_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    from seo_marketing.capabilities import CAP_SEO_OPTIMIZATION_PLAN

    return _write_desc(
        tool_id=TOOL_SEO_OPTIMIZATION_MEASURE,
        name="SEO Optimization Measure",
        description="Measure optimization action",
        category="seo",
        adapter_id="seo_marketing",
        capabilities=(CAP_SEO_OPTIMIZATION_PLAN,),
        operations=("optimization_measure",),
        enabled=enabled,
    )


def seo_optimization_decide_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    from seo_marketing.capabilities import CAP_SEO_OPTIMIZATION_PLAN

    return _write_desc(
        tool_id=TOOL_SEO_OPTIMIZATION_DECIDE,
        name="SEO Optimization Decide",
        description="Decide optimization outcome",
        category="seo",
        adapter_id="seo_marketing",
        capabilities=(CAP_SEO_OPTIMIZATION_PLAN,),
        operations=("optimization_decide",),
        enabled=enabled,
    )


def b2b_supplier_create_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    from b2b_commerce.capabilities import CAP_B2B_SUPPLIER_WRITE

    return _write_desc(
        tool_id=TOOL_B2B_SUPPLIER_CREATE,
        name="B2B Supplier Create",
        description="Create tenant-scoped supplier",
        category="b2b",
        adapter_id="b2b_commerce",
        capabilities=(CAP_B2B_SUPPLIER_WRITE,),
        operations=("create_supplier", "supplier.create"),
        enabled=enabled,
    )


def b2b_supplier_get_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    from b2b_commerce.capabilities import CAP_B2B_SUPPLIER_READ

    return _read_desc(
        tool_id=TOOL_B2B_SUPPLIER_GET,
        name="B2B Supplier Get",
        description="Read supplier by id",
        category="b2b",
        adapter_id="b2b_commerce",
        capabilities=(CAP_B2B_SUPPLIER_READ,),
        operations=("get_supplier", "supplier.get"),
        enabled=enabled,
    )


def b2b_wholesale_ingest_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    from b2b_commerce.capabilities import CAP_B2B_WHOLESALE_INGEST

    return _write_desc(
        tool_id=TOOL_B2B_WHOLESALE_INGEST,
        name="B2B Wholesale Ingest",
        description="Ingest supplier price list via governed path",
        category="b2b",
        adapter_id="b2b_commerce",
        capabilities=(CAP_B2B_WHOLESALE_INGEST,),
        operations=("ingest", "wholesale.ingest"),
        enabled=enabled,
        timeout=120.0,
    )


def b2b_wholesale_list_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    from b2b_commerce.capabilities import CAP_B2B_WHOLESALE_READ

    return _read_desc(
        tool_id=TOOL_B2B_WHOLESALE_LIST,
        name="B2B Wholesale List",
        description="List wholesale offers",
        category="b2b",
        adapter_id="b2b_commerce",
        capabilities=(CAP_B2B_WHOLESALE_READ,),
        operations=("list_wholesale", "wholesale.list"),
        enabled=enabled,
    )


def b2b_wholesale_compare_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    from b2b_commerce.capabilities import CAP_B2B_WHOLESALE_COMPARE

    return _read_desc(
        tool_id=TOOL_B2B_WHOLESALE_COMPARE,
        name="B2B Wholesale Compare",
        description="Deterministic wholesale offer comparison",
        category="b2b",
        adapter_id="b2b_commerce",
        capabilities=(CAP_B2B_WHOLESALE_COMPARE,),
        operations=("compare", "wholesale.compare"),
        enabled=enabled,
    )


def b2b_wholesale_changes_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    from b2b_commerce.capabilities import CAP_B2B_WHOLESALE_READ

    return _read_desc(
        tool_id=TOOL_B2B_WHOLESALE_CHANGES,
        name="B2B Wholesale Changes",
        description="Detect supplier price changes across versions",
        category="b2b",
        adapter_id="b2b_commerce",
        capabilities=(CAP_B2B_WHOLESALE_READ,),
        operations=("changes", "wholesale.changes"),
        enabled=enabled,
    )


def b2b_inquiry_create_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    from b2b_commerce.capabilities import CAP_TELEGRAM_READ, CAP_B2B_ASSISTANT_USE

    return _write_desc(
        tool_id=TOOL_B2B_INQUIRY_CREATE,
        name="B2B Inquiry Create",
        description="Process inbound Telegram inquiry",
        category="b2b",
        adapter_id="b2b_commerce",
        capabilities=(CAP_TELEGRAM_READ, CAP_B2B_ASSISTANT_USE),
        operations=("inquiry.create",),
        enabled=enabled,
    )


def b2b_quote_create_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    from b2b_commerce.capabilities import CAP_B2B_QUOTE_CREATE

    return _write_desc(
        tool_id=TOOL_B2B_QUOTE_CREATE,
        name="B2B Quote Create",
        description="Create commercial quote version",
        category="b2b",
        adapter_id="b2b_commerce",
        capabilities=(CAP_B2B_QUOTE_CREATE,),
        operations=("create_quote", "quote.create"),
        enabled=enabled,
    )


def b2b_quote_get_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    from b2b_commerce.capabilities import CAP_B2B_QUOTE_READ

    return _read_desc(
        tool_id=TOOL_B2B_QUOTE_GET,
        name="B2B Quote Get",
        description="Read quote version",
        category="b2b",
        adapter_id="b2b_commerce",
        capabilities=(CAP_B2B_QUOTE_READ,),
        operations=("quote.get",),
        enabled=enabled,
    )


def b2b_quote_send_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    from b2b_commerce.capabilities import CAP_B2B_QUOTE_SEND

    return _write_desc(
        tool_id=TOOL_B2B_QUOTE_SEND,
        name="B2B Quote Send",
        description="Send approved quote via Telegram side effect",
        category="b2b",
        adapter_id="b2b_commerce",
        capabilities=(CAP_B2B_QUOTE_SEND,),
        operations=("send_quote", "quote.send"),
        enabled=enabled,
        network=True,
    )


def b2b_order_draft_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    from b2b_commerce.capabilities import CAP_B2B_ORDER_DRAFT

    return _write_desc(
        tool_id=TOOL_B2B_ORDER_DRAFT,
        name="B2B Order Draft",
        description="Create order draft from quote",
        category="b2b",
        adapter_id="b2b_commerce",
        capabilities=(CAP_B2B_ORDER_DRAFT,),
        operations=("create_order_draft", "order.draft"),
        enabled=enabled,
    )


def b2b_order_submit_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    from b2b_commerce.capabilities import CAP_B2B_ORDER_SUBMIT

    return _write_desc(
        tool_id=TOOL_B2B_ORDER_SUBMIT,
        name="B2B Order Submit",
        description="Submit confirmed order draft",
        category="b2b",
        adapter_id="b2b_commerce",
        capabilities=(CAP_B2B_ORDER_SUBMIT,),
        operations=("submit_order", "order.submit"),
        enabled=enabled,
        network=True,
    )


def b2b_handoff_create_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    from b2b_commerce.capabilities import CAP_B2B_ASSISTANT_USE

    return _write_desc(
        tool_id=TOOL_B2B_HANDOFF_CREATE,
        name="B2B Handoff Create",
        description="Human handoff for policy/ambiguity",
        category="b2b",
        adapter_id="b2b_commerce",
        capabilities=(CAP_B2B_ASSISTANT_USE,),
        operations=("handoff.create",),
        enabled=enabled,
    )


def b2b_assistant_use_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    from b2b_commerce.capabilities import CAP_B2B_ASSISTANT_USE

    return _write_desc(
        tool_id=TOOL_B2B_ASSISTANT_USE,
        name="B2B Assistant Use",
        description="Governed B2B sales assistant",
        category="b2b",
        adapter_id="b2b_commerce",
        capabilities=(CAP_B2B_ASSISTANT_USE,),
        operations=("assistant_process", "assistant.use"),
        enabled=enabled,
    )


def telegram_message_send_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    from b2b_commerce.capabilities import CAP_TELEGRAM_SEND

    return _write_desc(
        tool_id=TOOL_TELEGRAM_MESSAGE_SEND,
        name="Telegram Message Send",
        description="Send Telegram message via governed provider",
        category="telegram",
        adapter_id="b2b_commerce",
        capabilities=(CAP_TELEGRAM_SEND,),
        operations=("send_message", "message.send"),
        enabled=enabled,
        network=True,
    )


def telegram_conversation_get_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    from b2b_commerce.capabilities import CAP_B2B_READ

    return _read_desc(
        tool_id=TOOL_TELEGRAM_CONVERSATION_GET,
        name="Telegram Conversation Get",
        description="Read B2B conversation state",
        category="telegram",
        adapter_id="b2b_commerce",
        capabilities=(CAP_B2B_READ,),
        operations=("get_conversation", "conversation.get"),
        enabled=enabled,
    )


def external_api_request_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_EXTERNAL_API,
        name="External API Request",
        description="Allowlisted external API request (1C/generic foundation)",
        category="external_api",
        adapter_id="external_api",
        capabilities=(CAP_EXTERNAL_READ,),
        operations=("request", "get"),
        enabled=enabled,
        timeout=30.0,
        network=True,
    )


def web_search_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_WEB_SEARCH,
        name="Web Search Query",
        description="Web search contract (may wrap SearchReadAdapter)",
        category="web_search",
        adapter_id="web_search",
        capabilities=(CAP_EXTERNAL_READ,),
        operations=("query", "search"),
        enabled=enabled,
        timeout=DEFAULT_SEARCH_TIMEOUT_SECONDS,
        network=True,
    )


def database_read_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_DB_READ,
        name="Database Read",
        description="Parameterized read against allowlisted datasource",
        category="database",
        adapter_id="database",
        capabilities=(CAP_DB_READ,),
        operations=("select", "read", "query"),
        enabled=enabled,
        timeout=15.0,
    )


def database_write_descriptor(*, enabled: bool = False) -> ToolDescriptor:
    return _write_desc(
        tool_id=TOOL_DB_WRITE,
        name="Database Write",
        description="Mutating DB ops via SideEffectExecutor only",
        category="database",
        adapter_id="database",
        capabilities=(CAP_DB_WRITE,),
        operations=("insert", "update", "write"),
        enabled=enabled,
        network=False,
    )


def knowledge_ingest_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _write_desc(
        tool_id=TOOL_KNOWLEDGE_INGEST,
        name="Knowledge Ingest",
        description="Governed knowledge ingestion with batch admission",
        category="knowledge",
        adapter_id="knowledge",
        capabilities=(CAP_FILESYSTEM_WRITE,),
        operations=("ingest",),
        enabled=enabled,
        timeout=120.0,
    )


def knowledge_retrieve_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_KNOWLEDGE_RETRIEVE,
        name="Knowledge Retrieve",
        description="Bounded tenant-scoped RAG retrieval",
        category="knowledge",
        adapter_id="knowledge",
        capabilities=(CAP_FILESYSTEM_READ,),
        operations=("retrieve",),
        enabled=enabled,
        timeout=30.0,
    )


def knowledge_delete_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _write_desc(
        tool_id=TOOL_KNOWLEDGE_DELETE,
        name="Knowledge Delete",
        description="Authorized knowledge tombstone/deletion",
        category="knowledge",
        adapter_id="knowledge",
        capabilities=(CAP_FILESYSTEM_WRITE,),
        operations=("delete",),
        enabled=enabled,
        timeout=60.0,
    )


def knowledge_status_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_KNOWLEDGE_STATUS,
        name="Knowledge Status",
        description="Knowledge platform health/status",
        category="knowledge",
        adapter_id="knowledge",
        capabilities=(CAP_FILESYSTEM_READ,),
        operations=("status",),
        enabled=enabled,
        timeout=5.0,
    )


def memory_write_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _write_desc(
        tool_id=TOOL_MEMORY_WRITE,
        name="Memory Write",
        description="Controlled durable memory write via policy",
        category="memory",
        adapter_id="knowledge",
        capabilities=(CAP_FILESYSTEM_WRITE,),
        operations=("write",),
        enabled=enabled,
        timeout=30.0,
    )


def memory_read_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_MEMORY_READ,
        name="Memory Read",
        description="Tenant-scoped memory retrieval",
        category="memory",
        adapter_id="knowledge",
        capabilities=(CAP_FILESYSTEM_READ,),
        operations=("read",),
        enabled=enabled,
        timeout=30.0,
    )


def memory_propose_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_MEMORY_PROPOSE,
        name="Memory Propose",
        description="Evaluate memory write policy without persisting",
        category="memory",
        adapter_id="knowledge",
        capabilities=(CAP_FILESYSTEM_READ,),
        operations=("propose",),
        enabled=enabled,
        timeout=10.0,
    )


def content_research_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _write_desc(
        tool_id=TOOL_CONTENT_RESEARCH,
        name="Content Research",
        description="Governed content research with evidence provenance",
        category="content",
        adapter_id="content_intel",
        capabilities=(CAP_EXTERNAL_READ,),
        operations=("research",),
        enabled=enabled,
        timeout=120.0,
    )


def content_strategy_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _write_desc(
        tool_id=TOOL_CONTENT_STRATEGY,
        name="Content Strategy",
        description="Create versioned content strategy",
        category="content",
        adapter_id="content_intel",
        capabilities=(CAP_FILESYSTEM_WRITE,),
        operations=("create_strategy",),
        enabled=enabled,
        timeout=60.0,
    )


def content_generate_copy_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _write_desc(
        tool_id=TOOL_CONTENT_GENERATE_COPY,
        name="Content Generate Copy",
        description="Generate validated copy/product content",
        category="content",
        adapter_id="content_intel",
        capabilities=(CAP_FILESYSTEM_WRITE,),
        operations=("generate_copy",),
        enabled=enabled,
        timeout=90.0,
    )


def content_generate_media_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _write_desc(
        tool_id=TOOL_CONTENT_GENERATE_MEDIA,
        name="Content Generate Media",
        description="Media brief and governed media generation",
        category="content",
        adapter_id="content_intel",
        capabilities=(CAP_IMAGE_GENERATE,),
        operations=("generate_media",),
        enabled=enabled,
        timeout=120.0,
    )


def content_publication_plan_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _write_desc(
        tool_id=TOOL_CONTENT_PUBLICATION_PLAN,
        name="Content Publication Plan",
        description="Versioned timezone-safe publication planning",
        category="content",
        adapter_id="content_intel",
        capabilities=(CAP_FILESYSTEM_WRITE,),
        operations=("create_publication_plan",),
        enabled=enabled,
        timeout=60.0,
    )


def content_analyze_performance_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_CONTENT_ANALYZE_PERFORMANCE,
        name="Content Analyze Performance",
        description="Deterministic content performance analytics",
        category="content",
        adapter_id="content_intel",
        capabilities=(CAP_EXTERNAL_READ,),
        operations=("analyze_performance",),
        enabled=enabled,
        timeout=60.0,
    )


def content_optimize_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _write_desc(
        tool_id=TOOL_CONTENT_OPTIMIZE,
        name="Content Optimize",
        description="Evidence-gated content optimization decision",
        category="content",
        adapter_id="content_intel",
        capabilities=(CAP_FILESYSTEM_WRITE,),
        operations=("optimize",),
        enabled=enabled,
        timeout=60.0,
    )


def content_get_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_CONTENT_GET,
        name="Content Get",
        description="Retrieve content asset by version",
        category="content",
        adapter_id="content_intel",
        capabilities=(CAP_FILESYSTEM_READ,),
        operations=("get",),
        enabled=enabled,
        timeout=10.0,
    )


def content_status_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_CONTENT_STATUS,
        name="Content Status",
        description="Content platform health",
        category="content",
        adapter_id="content_intel",
        capabilities=(CAP_FILESYSTEM_READ,),
        operations=("status",),
        enabled=enabled,
        timeout=5.0,
    )


def media_ingest_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _write_desc(
        tool_id=TOOL_MEDIA_INGEST,
        name="Media Ingest",
        description="Governed product media ingestion",
        category="media",
        adapter_id="product_media",
        capabilities=(CAP_FILESYSTEM_WRITE,),
        operations=("ingest",),
        enabled=enabled,
        timeout=60.0,
    )


def media_get_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_MEDIA_GET,
        name="Media Get",
        description="Retrieve media version",
        category="media",
        adapter_id="product_media",
        capabilities=(CAP_FILESYSTEM_READ,),
        operations=("get",),
        enabled=enabled,
        timeout=10.0,
    )


def media_analyze_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_MEDIA_ANALYZE,
        name="Media Analyze",
        description="Metadata and quality analysis",
        category="media",
        adapter_id="product_media",
        capabilities=(CAP_FILESYSTEM_READ,),
        operations=("analyze",),
        enabled=enabled,
        timeout=60.0,
    )


def media_generate_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _write_desc(
        tool_id=TOOL_MEDIA_GENERATE,
        name="Media Generate",
        description="Governed image generation",
        category="media",
        adapter_id="product_media",
        capabilities=(CAP_IMAGE_GENERATE,),
        operations=("generate",),
        enabled=enabled,
        timeout=120.0,
    )


def media_transform_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _write_desc(
        tool_id=TOOL_MEDIA_TRANSFORM,
        name="Media Transform",
        description="Resize/crop/thumbnail/background operations",
        category="media",
        adapter_id="product_media",
        capabilities=(CAP_IMAGE_GENERATE, CAP_FILESYSTEM_WRITE),
        operations=("transform",),
        enabled=enabled,
        timeout=90.0,
    )


def media_delete_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _write_desc(
        tool_id=TOOL_MEDIA_DELETE,
        name="Media Delete",
        description="Tombstone media version",
        category="media",
        adapter_id="product_media",
        capabilities=(CAP_FILESYSTEM_WRITE,),
        operations=("delete",),
        enabled=enabled,
        timeout=30.0,
    )


def media_link_product_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _write_desc(
        tool_id=TOOL_MEDIA_LINK_PRODUCT,
        name="Media Link Product",
        description="Associate media with product/SKU",
        category="media",
        adapter_id="product_media",
        capabilities=(CAP_FILESYSTEM_WRITE,),
        operations=("link_product",),
        enabled=enabled,
        timeout=30.0,
    )


def media_find_similar_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_MEDIA_FIND_SIMILAR,
        name="Media Find Similar",
        description="Tenant-scoped similarity lookup",
        category="media",
        adapter_id="product_media",
        capabilities=(CAP_FILESYSTEM_READ,),
        operations=("find_similar",),
        enabled=enabled,
        timeout=60.0,
    )


def media_validate_set_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_MEDIA_VALIDATE_SET,
        name="Media Validate Set",
        description="Validate product media set requirements",
        category="media",
        adapter_id="product_media",
        capabilities=(CAP_FILESYSTEM_READ,),
        operations=("validate_set",),
        enabled=enabled,
        timeout=60.0,
    )


def commerce_catalog_analyze_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    return _read_desc(
        tool_id=TOOL_COMMERCE_CATALOG_ANALYZE,
        name="Commerce Catalog Analyze",
        description="Deterministic catalog quality analysis",
        category="commerce",
        adapter_id="product_platform",
        capabilities=(CAP_FILESYSTEM_READ,),
        operations=("analyze",),
        enabled=enabled,
        timeout=60.0,
    )


def commerce_product_import_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    from commerce.capabilities import CAP_CATALOG_WRITE

    return _write_desc(
        tool_id=TOOL_COMMERCE_PRODUCT_IMPORT,
        name="Commerce Product Import",
        description="Governed product import with dry-run",
        category="commerce",
        adapter_id="product_platform",
        capabilities=(CAP_CATALOG_WRITE,),
        operations=("import", "import_preview"),
        enabled=enabled,
        timeout=120.0,
    )


def commerce_price_decide_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    from commerce.capabilities import CAP_PRICING_PROPOSE

    return _write_desc(
        tool_id=TOOL_COMMERCE_PRICE_DECIDE,
        name="Commerce Price Decide",
        description="Policy-driven price decision",
        category="commerce",
        adapter_id="product_platform",
        capabilities=(CAP_PRICING_PROPOSE,),
        operations=("decide_price",),
        enabled=enabled,
        timeout=60.0,
    )


def commerce_price_apply_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    from commerce.capabilities import CAP_PRICING_WRITE

    return _write_desc(
        tool_id=TOOL_COMMERCE_PRICE_APPLY,
        name="Commerce Price Apply",
        description="Apply approved price decision",
        category="commerce",
        adapter_id="product_platform",
        capabilities=(CAP_PRICING_WRITE,),
        operations=("apply_price",),
        enabled=enabled,
        timeout=60.0,
        critical=True,
    )


def commerce_cms_create_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    from commerce.capabilities import CAP_CATALOG_WRITE

    return _write_desc(
        tool_id=TOOL_COMMERCE_CMS_CREATE,
        name="Commerce CMS Create Product",
        description="Governed CMS product create",
        category="commerce",
        adapter_id="product_platform",
        capabilities=(CAP_CATALOG_WRITE,),
        operations=("cms_create",),
        enabled=enabled,
        timeout=90.0,
        critical=True,
    )


def commerce_cms_update_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    from commerce.capabilities import CAP_CATALOG_WRITE

    return _write_desc(
        tool_id=TOOL_COMMERCE_CMS_UPDATE,
        name="Commerce CMS Update Product",
        description="Governed CMS product update",
        category="commerce",
        adapter_id="product_platform",
        capabilities=(CAP_CATALOG_WRITE,),
        operations=("cms_update_product",),
        enabled=enabled,
        timeout=90.0,
        critical=True,
    )


def commerce_cms_archive_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    from commerce.capabilities import CAP_CATALOG_WRITE

    return _write_desc(
        tool_id=TOOL_COMMERCE_CMS_ARCHIVE,
        name="Commerce CMS Archive Product",
        description="Governed CMS product archive",
        category="commerce",
        adapter_id="product_platform",
        capabilities=(CAP_CATALOG_WRITE,),
        operations=("cms_archive_product",),
        enabled=enabled,
        timeout=90.0,
        critical=True,
    )


def commerce_cms_stock_update_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    from commerce.capabilities import CAP_STOCK_WRITE

    return _write_desc(
        tool_id=TOOL_COMMERCE_CMS_STOCK_UPDATE,
        name="Commerce CMS Stock Update",
        description="Governed CMS stock sync from trusted inventory",
        category="commerce",
        adapter_id="product_platform",
        capabilities=(CAP_STOCK_WRITE,),
        operations=("cms_update_stock",),
        enabled=enabled,
        timeout=90.0,
        critical=True,
    )


def commerce_order_ingest_descriptor(*, enabled: bool = True) -> ToolDescriptor:
    from commerce.capabilities import CAP_ORDER_WRITE

    return _write_desc(
        tool_id=TOOL_COMMERCE_ORDER_INGEST,
        name="Commerce Order Ingest",
        description="Idempotent external order ingestion",
        category="commerce",
        adapter_id="product_platform",
        capabilities=(CAP_ORDER_WRITE,),
        operations=("ingest_order",),
        enabled=enabled,
        timeout=60.0,
    )

