"""Data Intelligence service facade."""

from __future__ import annotations

import re
from dataclasses import replace

from data_intel.analysis import analyze_margin, detect_anomalies
from data_intel.business_process import (
    assess_structure_for_process,
    basic_margin,
    build_business_workbook,
    find_conflicting_identifier_duplicates,
    merge_with_provenance,
    price_comparison_changed_only,
    run_economics_batch,
    stock_reconciliation_report,
)
from data_intel.economics import EconomicsPolicy
from data_intel.cleaning import clean_row, normalize_decimal_string
from data_intel.compare import compare_price_lists, reconcile_stock
from data_intel.contracts import (
    ROLE_ARTICLE,
    ROLE_AVAILABLE_STOCK,
    ROLE_BARCODE,
    ROLE_BRAND,
    ROLE_CATEGORY,
    ROLE_EAN,
    ROLE_PRICE,
    ROLE_PRODUCT_NAME,
    ROLE_PURCHASE_PRICE,
    ROLE_SELLING_PRICE,
    ROLE_SKU,
    ROLE_STOCK,
    DataRow,
    DataTransformation,
    DatasetDescriptor,
    new_id,
    row_ref,
    utc_now,
)
from data_intel.counterparty import match_counterparties
from data_intel.duplicates import find_duplicates
from data_intel.errors import (
    DATASET_ACCESS_DENIED,
    DATASET_BATCH_REQUIRED,
    DATASET_NOT_FOUND,
    DATASET_PARSE_FAILED,
    DATASET_TOO_LARGE,
    LARGE_DATASET_WORKFLOW_UNAVAILABLE,
    DataIntelError,
)
from data_intel.planner import assert_sync_data_allowed, plan_data_job
from data_intel.excel_out import (
    generate_comparison_workbook,
    generate_searchable_payments_workbook,
    generate_workbook,
)
from data_intel.ingest import ingest_bytes
from data_intel.large import LargeDatasetPolicy, large_dataset_execution_key
from data_intel.mapping import role_map
from data_intel.merge import merge_datasets
from data_intel.nl_ops import (
    AmbiguousOperationError,
    UnsupportedOperationError,
    compile_request,
)
from data_intel.product_match import match_products
from data_intel.quality import build_quality_report
from data_intel.query import aggregate, pivot_report, search_rows
from data_intel.reconcile import reconcile_payments, reconcile_vat_amounts
from data_intel.store import InMemoryDatasetStore
from data_intel.transform import execute_plan
from data_intel.workflow_def import register_data_intel_workflows
from security.tenant import normalize_tenant_id

_PREVIEW_ROW_LIMIT = 5
_PREVIEW_INTERNAL_PREFIX = "__"

# Production defect closure (XLSX attachment -> failed response, phase 2):
# a request that both references a specific product/SKU AND asks Panda to
# act on it (e.g. "prepare it for Bitrix/Aspro") is not a supported
# nl_ops transform (filter/sort/percent/etc.) -- it previously always fell
# through to ``_analyze_only_summary``'s dimension-only text, silently
# discarding the actually-parsed row data. This is a generic, schema-driven
# lookup (identifying columns come from the already-detected table schema,
# never a hardcoded product/workbook) that locates the single row the free
# text is actually about and surfaces its real values instead.
_PRODUCT_ID_ROLES = (ROLE_SKU, ROLE_ARTICLE, ROLE_EAN, ROLE_PRODUCT_NAME)
_PRICE_LOOKUP_ROLES = (ROLE_PURCHASE_PRICE, ROLE_SELLING_PRICE, ROLE_PRICE)
_STOCK_LOOKUP_ROLES = (ROLE_STOCK, ROLE_AVAILABLE_STOCK)
# Product-preview card fields (production hotfix: the row-lookup result was
# a vague "prepared a card and an action plan" sentence that never actually
# showed the card -- this renders the real, present-in-schema fields only;
# a role with no matching column in THIS workbook is simply omitted, never
# invented. Order here is the card's display order.
_CARD_FIELD_ROLES = (
    (ROLE_PRODUCT_NAME, "Наименование"),
    (ROLE_SKU, "Артикул/SKU"),
    (ROLE_ARTICLE, "Артикул"),
    (ROLE_EAN, "EAN"),
    (ROLE_BARCODE, "Штрихкод"),
    (ROLE_CATEGORY, "Категория"),
    (ROLE_BRAND, "Бренд"),
)
_MIN_IDENTIFIER_MATCH_LEN = 4
_USER_SUPPLIED_PRICE_RE = re.compile(
    r"(розничн\w*|продажн\w*|retail|selling)\D{0,20}?(\d[\d\s]*(?:[.,]\d+)?)", re.I
)


def _find_row_by_identifier(text: str, rows: list[dict], table) -> tuple[dict, str, str] | None:
    blob = (text or "").casefold()
    candidates = [c for c in table.columns if c.semantic_role in _PRODUCT_ID_ROLES]
    if not candidates:
        return None
    matches: list[tuple[dict, str, str]] = []
    for row in rows:
        for col in candidates:
            value = str(row.get(col.source_name) or "").strip()
            if len(value) < _MIN_IDENTIFIER_MATCH_LEN:
                continue
            if value.casefold() in blob:
                matches.append((row, col.source_name, value))
                break
    # Only act on an unambiguous single-row match; anything else (no match,
    # or several rows matching) falls back to the existing analyze-only
    # summary unchanged.
    if len(matches) == 1:
        return matches[0]
    return None


def _extract_user_supplied_price(text: str) -> str | None:
    match = _USER_SUPPLIED_PRICE_RE.search(text or "")
    if not match:
        return None
    return normalize_decimal_string(match.group(2))


def _role_value(row: dict, table, role: str) -> str:
    col = next((c for c in table.columns if c.semantic_role == role), None)
    if col is None:
        return ""
    value = row.get(col.source_name)
    return str(value) if value not in (None, "") else ""

_OP_HUMAN_RU = {
    "filter_contains": "фильтр по тексту",
    "filter_compare": "фильтр по цене",
    "sort": "сортировка",
    "limit": "ограничение количества строк",
    "percent_round": "изменение цены на процент с округлением",
    "add_column_percent": "добавление вычисляемого столбца",
    "remove_column": "удаление столбца",
    "rename_column": "переименование столбца",
    "dedup": "поиск дубликатов",
}


class DataIntelligenceService:
    def __init__(
        self,
        store=None,
        *,
        large_policy: LargeDatasetPolicy | None = None,
        workflow_runtime=None,
        document_service=None,
        observability=None,
        artifact_service=None,
    ):
        self.store = store or InMemoryDatasetStore()
        self.large_policy = large_policy or LargeDatasetPolicy()
        self.workflow_runtime = workflow_runtime
        self.document_service = document_service
        self.observability = observability
        # Block 5.1 artifact integration: optional, may be wired post-construction
        # (see main.py) once the canonical ArtifactService is available -- generated
        # workbooks register through it instead of only the data_intel blob store.
        self.artifact_service = artifact_service
        if workflow_runtime is not None:
            try:
                register_data_intel_workflows(
                    workflow_runtime.definitions, workflow_runtime.platform
                )
            except Exception:
                pass

    def _emit(self, event: str, **meta):
        obs = self.observability
        if obs is None:
            return
        try:
            ctx = obs.create_context(workflow_id="", task_id="data-intel")
            safe = {k: v for k, v in meta.items() if k not in {"rows", "raw", "content"}}
            obs.emit(event, context=ctx, component="data_intelligence", metadata=safe)
        except Exception:
            pass

    def ingest(
        self,
        data: bytes,
        *,
        filename: str,
        tenant_id: str,
        source_document_id: str = "",
        enqueue_large: bool = True,
    ) -> dict:
        tenant = normalize_tenant_id(tenant_id)
        result = ingest_bytes(
            data,
            filename=filename,
            tenant_id=tenant,
            source_document_id=source_document_id,
        )
        desc = result.descriptor
        # Attach lineage refs
        rows_by_table = {}
        for tid, rows in result.table_rows.items():
            out = []
            roles = {}
            table = next((t for t in desc.tables if t.table_id == tid), None)
            if table:
                roles = role_map(table.columns)
            for r in rows:
                src = int(r.get("__source_row") or 0)
                values = {k: v for k, v in r.items() if k != "__source_row"}
                cleaned, raw = clean_row(values, roles=roles)
                # Map role aliases onto row for search
                for col, role in roles.items():
                    if role and role != "unknown" and col in cleaned:
                        cleaned.setdefault(role, cleaned[col])
                cleaned["__source_row"] = src
                cleaned["__row_ref"] = row_ref(desc.dataset_id, tid, src)
                cleaned["__raw"] = raw
                out.append(cleaned)
            rows_by_table[tid] = out

        cell_count = sum(
            len(rows) * max((len(rows[0]) if rows else 0), 1) for rows in rows_by_table.values()
        )

        self.store.save_dataset(desc, rows_by_table)
        self._emit(
            "data.dataset_ingested",
            dataset_id=desc.dataset_id,
            rows=desc.row_count,
            columns=desc.column_count,
            sheets=len(desc.sheets),
            tenant=tenant,
            format=desc.format,
        )

        async_needed = self.large_policy.requires_async(
            row_count=desc.row_count,
            cell_count=cell_count,
            size_bytes=len(data),
        )
        workflow_id = None
        if async_needed:
            if not enqueue_large:
                raise DataIntelError(DATASET_BATCH_REQUIRED)
            workflow_id = self._enqueue_large(desc)
            desc = replace(desc, status="async_processing")
            self.store.save_dataset(desc, rows_by_table)

        return {
            "dataset_id": desc.dataset_id,
            "descriptor": desc,
            "async": bool(workflow_id),
            "workflow_id": workflow_id,
            "tables": [
                {
                    "table_id": t.table_id,
                    "sheet": t.sheet,
                    "header_row": t.header_row,
                    "row_count": t.row_count,
                    "confidence": t.confidence,
                    "unresolved": t.unresolved,
                    "columns": [
                        {
                            "name": c.source_name,
                            "role": c.semantic_role,
                            "type": c.inferred_type,
                            "confidence": c.confidence,
                        }
                        for c in t.columns
                    ],
                }
                for t in desc.tables
            ],
        }

    def ingest_from_document(self, document_id: str, *, tenant_id: str, filename: str = "") -> dict:
        if self.document_service is None:
            raise DataIntelError("dataset_store_unavailable")
        from memory.models import MemoryScope

        scope = MemoryScope(scope_type="workspace", scope_id=tenant_id, tenant_ref=tenant_id)
        row = self.document_service.get(document_id, requesting_scope=scope)
        if row is None:
            raise DataIntelError(DATASET_ACCESS_DENIED)
        blob = None
        if hasattr(self.document_service.store, "get_blob"):
            blob = self.document_service.store.get_blob(document_id)
        if not blob:
            raise DataIntelError(DATASET_NOT_FOUND)
        return self.ingest(
            blob,
            filename=filename or row.filename_safe or "document.bin",
            tenant_id=tenant_id,
            source_document_id=document_id,
        )

    def _enqueue_large(self, desc: DatasetDescriptor) -> str:
        if self.workflow_runtime is None:
            raise DataIntelError(LARGE_DATASET_WORKFLOW_UNAVAILABLE)
        exec_key = large_dataset_execution_key(desc.tenant_id, desc.dataset_id)
        existing = self.workflow_runtime.state_manager.find_by_execution_key(
            exec_key, tenant_id=desc.tenant_id
        )
        if existing is not None:
            return existing.workflow_id

        async def _create():
            return await self.workflow_runtime.create_and_enqueue(
                "data.large_process",
                "1",
                execution_key=exec_key,
                tenant_id=desc.tenant_id,
                metadata={
                    "dataset_id": desc.dataset_id,
                    "tenant_id": desc.tenant_id,
                    "row_count": desc.row_count,
                    "rows_per_batch": self.large_policy.rows_per_batch,
                },
            )

        import asyncio

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # sync path: create instance without await via platform
                created = self.workflow_runtime.create_workflow(
                    "data.large_process",
                    "1",
                    execution_key=exec_key,
                    tenant_id=desc.tenant_id,
                    metadata={
                        "dataset_id": desc.dataset_id,
                        "tenant_id": desc.tenant_id,
                        "row_count": desc.row_count,
                        "rows_per_batch": self.large_policy.rows_per_batch,
                    },
                )
                wid = created["workflow_id"] if isinstance(created, dict) else created.workflow_id
                self.workflow_runtime.enqueue_existing(wid, idempotent=True)
                return wid
            result = loop.run_until_complete(_create())
            return result["workflow_id"]
        except Exception:
            created = self.workflow_runtime.create_workflow(
                "data.large_process",
                "1",
                execution_key=exec_key,
                tenant_id=desc.tenant_id,
                metadata={
                    "dataset_id": desc.dataset_id,
                    "tenant_id": desc.tenant_id,
                    "row_count": desc.row_count,
                    "rows_per_batch": self.large_policy.rows_per_batch,
                },
            )
            wid = created["workflow_id"] if isinstance(created, dict) else getattr(created, "workflow_id", None)
            if wid:
                try:
                    self.workflow_runtime.enqueue_existing(wid, idempotent=True)
                except Exception:
                    pass
            if not wid:
                raise DataIntelError(LARGE_DATASET_WORKFLOW_UNAVAILABLE)
            return wid

    def profile(self, dataset_id: str, *, tenant_id: str) -> dict:
        desc = self.store.get_dataset(dataset_id, tenant_id=tenant_id)
        if desc is None:
            raise DataIntelError(DATASET_ACCESS_DENIED)
        rows = self.store.get_rows(dataset_id, tenant_id=tenant_id)
        return {
            "dataset_id": dataset_id,
            "row_count": len(rows),
            "tables": [
                {
                    "table_id": t.table_id,
                    "columns": [
                        {
                            "name": c.source_name,
                            "role": c.semantic_role,
                            "type": c.inferred_type,
                            "confidence": c.confidence,
                            "examples": list(c.examples_safe),
                        }
                        for c in t.columns
                    ],
                    "confidence": t.confidence,
                    "unresolved": t.unresolved,
                }
                for t in desc.tables
            ],
        }

    def normalize(self, dataset_id: str, *, tenant_id: str) -> dict:
        desc = self.store.get_dataset(dataset_id, tenant_id=tenant_id)
        if desc is None:
            raise DataIntelError(DATASET_ACCESS_DENIED)
        rows_by_table = {}
        for t in desc.tables:
            roles = role_map(t.columns)
            rows = self.store.get_rows(dataset_id, tenant_id=tenant_id, table_id=t.table_id)
            out = []
            for r in rows:
                values = {k: v for k, v in r.items() if not str(k).startswith("__")}
                cleaned, raw = clean_row(values, roles=roles)
                for col, role in roles.items():
                    if role != "unknown" and col in cleaned:
                        cleaned.setdefault(role, cleaned[col])
                cleaned["__source_row"] = r.get("__source_row")
                cleaned["__row_ref"] = r.get("__row_ref") or row_ref(
                    dataset_id, t.table_id, int(r.get("__source_row") or 0)
                )
                cleaned["__raw"] = raw
                out.append(cleaned)
            rows_by_table[t.table_id] = out
        updated = replace(desc, status="normalized")
        self.store.save_dataset(updated, rows_by_table)
        tx = DataTransformation(
            operation="normalize",
            input_refs=(dataset_id,),
            output_ref=dataset_id,
            provenance={"at": utc_now().isoformat()},
        )
        self.store.save_transformation(tenant_id, tx)
        return {"dataset_id": dataset_id, "status": "normalized", "row_count": sum(len(v) for v in rows_by_table.values())}

    def search(self, dataset_id: str, *, tenant_id: str, **kwargs) -> dict:
        rows = self.store.get_rows(dataset_id, tenant_id=tenant_id)
        return search_rows(rows, **kwargs)

    def match(self, left: dict, right: dict, *, entity_type: str = "counterparty") -> dict:
        if entity_type == "product":
            m = match_products(left, right)
        else:
            m = match_counterparties(left, right)
        return {
            "entity_type": m.entity_type,
            "method": m.match_method,
            "confidence": m.confidence,
            "same_entity": m.same_entity,
            "conflicts": list(m.conflicts),
            "review_required": m.review_required,
            "evidence": dict(m.evidence),
        }

    def compare_prices(self, left_rows: list[dict], right_rows: list[dict], **kwargs) -> dict:
        total = len(left_rows) + len(right_rows)
        assert_sync_data_allowed(row_count=total, operations=("compare",))
        return compare_price_lists(left_rows, right_rows, **kwargs)

    def reconcile(self, kind: str, left_rows: list[dict], right_rows: list[dict], **kwargs) -> dict:
        if kind == "stock":
            return reconcile_stock(left_rows, right_rows, **kwargs)
        if kind == "payment":
            return reconcile_payments(left_rows, right_rows, **kwargs)
        if kind == "vat":
            return reconcile_vat_amounts(left_rows, **kwargs)
        raise DataIntelError("reconciliation_conflict")

    def duplicates(self, dataset_id: str, *, tenant_id: str, business_keys: list[str] | None = None) -> list[dict]:
        rows = self.store.get_rows(dataset_id, tenant_id=tenant_id)
        return find_duplicates(rows, business_keys=business_keys)

    def merge(self, left_rows, right_rows, **kwargs) -> dict:
        total = len(left_rows) + len(right_rows)
        assert_sync_data_allowed(row_count=total, operations=("merge",))
        return merge_datasets(left_rows, right_rows, **kwargs)

    def aggregate(self, dataset_id: str, *, tenant_id: str, **kwargs) -> list[dict]:
        rows = self.store.get_rows(dataset_id, tenant_id=tenant_id)
        return aggregate(rows, **kwargs)

    def pivot(self, dataset_id: str, *, tenant_id: str, **kwargs) -> dict:
        rows = self.store.get_rows(dataset_id, tenant_id=tenant_id)
        return pivot_report(rows, **kwargs)

    def anomalies(self, dataset_id: str, *, tenant_id: str) -> list[dict]:
        rows = self.store.get_rows(dataset_id, tenant_id=tenant_id)
        refs = [str(r.get("__row_ref") or f"r{i}") for i, r in enumerate(rows)]
        issues = detect_anomalies(rows, row_refs=refs)
        return [
            {
                "row_ref": i.row_ref,
                "column": i.column,
                "issue_type": i.issue_type,
                "severity": i.severity,
                "description": i.description,
                "suggested_action": i.suggested_action,
            }
            for i in issues
        ]

    def margin(self, row: dict) -> dict:
        return analyze_margin(row)

    def generate_excel(
        self,
        dataset_id: str,
        *,
        tenant_id: str,
        kind: str = "data",
        comparison: dict | None = None,
    ) -> dict:
        desc = self.store.get_dataset(dataset_id, tenant_id=tenant_id)
        if desc is None:
            raise DataIntelError(DATASET_ACCESS_DENIED)
        rows = self.store.get_rows(dataset_id, tenant_id=tenant_id)
        assert_sync_data_allowed(row_count=len(rows), operations=("generate_xlsx",))
        if kind == "payments":
            data = generate_searchable_payments_workbook(rows)
            name = "report.xlsx"
        elif kind == "comparison":
            data = generate_comparison_workbook(comparison or {})
            name = "comparison.xlsx"
        else:
            if not rows:
                headers = ["empty"]
                body = []
                text_cols = set()
            else:
                headers = [k for k in rows[0].keys() if not str(k).startswith("__")]
                body = [[r.get(h) for h in headers] for r in rows]
                text_cols = {
                    i
                    for i, h in enumerate(headers)
                    if h in {"inn", "kpp", "ogrn", "ean", "sku", "article", "document_number"}
                }
            issues = self.anomalies(dataset_id, tenant_id=tenant_id)
            sheets = {
                "RESULT": {"headers": headers, "rows": body, "text_cols": text_cols},
                "ISSUES": {
                    "headers": ["row_ref", "column", "issue_type", "severity", "description"],
                    "rows": [
                        [x["row_ref"], x["column"], x["issue_type"], x["severity"], x["description"]]
                        for x in issues
                    ],
                },
            }
            data = generate_workbook(
                summary={"dataset_id": dataset_id, "rows": len(rows), "format": desc.format},
                sheets=sheets,
                provenance={
                    "dataset_id": dataset_id,
                    "source_document_id": desc.source_document_id,
                    "checksum": desc.checksum,
                    "original_preserved": True,
                },
            )
            name = "dataset.xlsx"
        self.store.save_blob(dataset_id, name, data, tenant_id=tenant_id)
        self._emit(
            "data.excel_generated",
            dataset_id=dataset_id,
            rows=len(rows),
            tenant=tenant_id,
            kind=kind,
        )
        return {"dataset_id": dataset_id, "filename": name, "size": len(data), "content": data}

    def _numeric_stat(self, rows: list[dict], column: str, op: str) -> str | None:
        out = aggregate(rows, group_by=[], measures={column: op})
        if not out:
            return None
        key = f"{column}_{op}"
        return out[0].get(key)

    def _bounded_preview(self, rows: list[dict], columns=(), limit: int = _PREVIEW_ROW_LIMIT) -> list[dict]:
        names = [c.source_name for c in columns] if columns else None
        out = []
        for r in rows[:limit]:
            if names:
                out.append({k: r.get(k) for k in names})
            else:
                out.append({k: v for k, v in r.items() if not str(k).startswith(_PREVIEW_INTERNAL_PREFIX)})
        return out

    def _build_summary_text(self, result, table) -> str:
        lines = [f"Строк было: {result.row_count_before}, стало: {result.row_count_after}."]
        if result.applied:
            ops_human = ", ".join(_OP_HUMAN_RU.get(a["op"], a["op"]) for a in result.applied)
            lines.append(f"Применено: {ops_human}.")
        if result.duplicate_groups:
            lines.append(f"Найдено групп потенциальных дублей: {len(result.duplicate_groups)}.")
        return " ".join(lines)

    def _analyze_only_summary(self, dataset_id: str, desc, rows: list[dict], table, *, tenant_id: str) -> dict:
        price_cols = [c for c in table.columns if c.semantic_role in (ROLE_PRICE, ROLE_SELLING_PRICE, ROLE_PURCHASE_PRICE)]
        stats: dict = {}
        lines = [f"В таблице {len(rows)} строк и {len(table.columns)} столбцов."]
        if price_cols:
            col = price_cols[0].source_name
            lo = self._numeric_stat(rows, col, "min")
            hi = self._numeric_stat(rows, col, "max")
            avg = self._numeric_stat(rows, col, "avg")
            if lo is not None and hi is not None:
                lines.append(f"Цена ({col}): от {lo} до {hi}, средняя {avg}.")
                stats = {"price_column": col, "min": lo, "max": hi, "avg": avg}
        duplicate_groups = find_duplicates(rows)
        if duplicate_groups:
            lines.append(f"Найдено групп потенциальных дублей: {len(duplicate_groups)}.")
        if table.unresolved:
            lines.append("Часть столбцов не удалось однозначно распознать.")
        return {
            "status": "ANALYZED",
            "dataset_id": dataset_id,
            "row_count": len(rows),
            "column_count": len(table.columns),
            "duplicate_groups_count": len(duplicate_groups),
            "stats": stats,
            "summary_text": " ".join(lines),
            "preview_rows": self._bounded_preview(rows, table.columns),
        }

    def _row_lookup_result(
        self, dataset_id: str, row_hit: tuple[dict, str, str], text: str, table
    ) -> dict:
        row, matched_column, matched_value = row_hit

        # Identifying/descriptive fields -- only ones actually present in
        # THIS workbook's schema, one line per role, never invented.
        card_lines: list[str] = []
        seen_roles: set[str] = set()
        for role, label in _CARD_FIELD_ROLES:
            if role in seen_roles:
                continue
            col = next((c for c in table.columns if c.semantic_role == role), None)
            if col is None:
                continue
            value = row.get(col.source_name)
            if value in (None, ""):
                continue
            card_lines.append(f"{label}: {value}")
            seen_roles.add(role)

        row_prices: dict = {}
        purchase_price = None
        selling_price_from_file = None
        for col in table.columns:
            if col.semantic_role not in _PRICE_LOOKUP_ROLES:
                continue
            value = row.get(col.source_name)
            if value in (None, ""):
                continue
            row_prices[col.source_name] = value
            if col.semantic_role == ROLE_PURCHASE_PRICE and purchase_price is None:
                purchase_price = value
            elif col.semantic_role == ROLE_SELLING_PRICE and selling_price_from_file is None:
                selling_price_from_file = value
        if purchase_price is not None:
            card_lines.append(f"Закупочная цена (из файла): {purchase_price}")

        # A retail price the USER supplied in this (or the merged prior)
        # turn always wins over one already in the file -- it is the value
        # the user explicitly asked Panda to use.
        user_price = _extract_user_supplied_price(text)
        retail_price = user_price if user_price is not None else selling_price_from_file
        if retail_price is not None:
            source = "из запроса" if user_price is not None else "из файла"
            card_lines.append(f"Розничная цена ({source}): {retail_price}")

        stock_value = None
        for role in _STOCK_LOOKUP_ROLES:
            col = next((c for c in table.columns if c.semantic_role == role), None)
            if col is None:
                continue
            value = row.get(col.source_name)
            if value not in (None, ""):
                stock_value = value
                break
        if stock_value is not None:
            card_lines.append(f"Остаток: {stock_value}")

        if not card_lines:
            card_lines.append(f"{matched_column}: {matched_value}")

        lines = ["Карточка товара (предпросмотр):"]
        lines.extend(f"- {ln}" for ln in card_lines)
        lines.append(
            "Статус: подготовлено для предпросмотра Bitrix/Aspro. "
            "Публикация/запись не выполнена — жду вашего подтверждения перед записью."
        )
        # Conversational glue for the governed single-product Bitrix write
        # (business_assistant.controlled_bitrix_write): a flat, already
        # role-resolved field dict so a later explicit approval turn
        # ("Подтверждаю: создай этот товар в Bitrix...") can build a
        # SingleProductWriteRequest from THIS SAME previewed row without
        # re-parsing the workbook or guessing which column is which --
        # persisted onto the conversation's ActiveTask by the caller
        # (WorkflowPandaConversationGateway._invoke_tool). Absent roles are
        # simply empty strings, never invented.
        product_fields = {
            "title": _role_value(row, table, ROLE_PRODUCT_NAME),
            "sku": _role_value(row, table, ROLE_SKU) or _role_value(row, table, ROLE_ARTICLE),
            "ean": _role_value(row, table, ROLE_EAN),
            "category": _role_value(row, table, ROLE_CATEGORY),
            "brand": _role_value(row, table, ROLE_BRAND),
            "purchase_price": str(purchase_price) if purchase_price not in (None, "") else "",
        }
        return {
            "status": "ROW_FOUND",
            "dataset_id": dataset_id,
            "matched_column": matched_column,
            "matched_value": matched_value,
            "row": {k: v for k, v in row.items() if not str(k).startswith(_PREVIEW_INTERNAL_PREFIX)},
            "row_prices": row_prices,
            "user_supplied_price": user_price,
            "product_fields": product_fields,
            "retail_price_preview": str(retail_price) if retail_price not in (None, "") else "",
            "summary_text": "\n".join(lines),
        }

    def execute_nl_request(self, dataset_id: str, text: str, *, tenant_id: str) -> dict:
        """Compile the free-text ``text`` into a bounded deterministic
        operation plan and apply it to ``dataset_id`` (Block 5.1 section 5/6).

        Never raises for ambiguity/unsupported requests -- returns a typed
        ``status`` instead so the caller (chat tool adapter) can route to the
        existing conversational clarification flow rather than guessing.
        """

        desc = self.store.get_dataset(dataset_id, tenant_id=tenant_id)
        if desc is None:
            raise DataIntelError(DATASET_ACCESS_DENIED)
        if not desc.tables:
            raise DataIntelError(DATASET_PARSE_FAILED)
        table = desc.tables[0]
        rows = self.store.get_rows(dataset_id, tenant_id=tenant_id, table_id=table.table_id)
        assert_sync_data_allowed(row_count=len(rows), operations=("analyze",))

        try:
            plan = compile_request(text, table)
        except AmbiguousOperationError as exc:
            return {
                "status": "AMBIGUOUS",
                "dataset_id": dataset_id,
                "message_safe": exc.message_safe,
                "candidates": list(exc.candidates),
            }
        except UnsupportedOperationError:
            row_hit = _find_row_by_identifier(text, rows, table)
            if row_hit is not None:
                return self._row_lookup_result(dataset_id, row_hit, text, table)
            return self._analyze_only_summary(dataset_id, desc, rows, table, tenant_id=tenant_id)

        result = execute_plan(rows, table.columns, plan)
        new_dataset_id = new_id("ds-")
        new_table = replace(table, columns=result.columns, row_count=len(result.rows))
        new_desc = DatasetDescriptor(
            dataset_id=new_dataset_id,
            tenant_id=tenant_id,
            source_document_id=desc.source_document_id,
            format=desc.format,
            sheets=desc.sheets,
            tables=(new_table,),
            row_count=len(result.rows),
            column_count=len(new_table.columns),
            checksum=desc.checksum,
            provenance={
                **{k: v for k, v in dict(desc.provenance).items()},
                "derived_from": dataset_id,
                "nl_request_operations": [a["op"] for a in result.applied],
            },
        )
        self.store.save_dataset(new_desc, {table.table_id: result.rows})
        tx = DataTransformation(
            operation="nl_request",
            input_refs=(dataset_id,),
            output_ref=new_dataset_id,
            parameters={"operations": result.applied},
            provenance={"text_len": len(text or "")},
        )
        self.store.save_transformation(tenant_id, tx)
        self._emit(
            "data.nl_operation_applied",
            dataset_id=new_dataset_id,
            source_dataset_id=dataset_id,
            operations=len(result.applied),
            rows_before=result.row_count_before,
            rows_after=result.row_count_after,
            tenant=tenant_id,
        )

        out = {
            "status": "OK",
            "dataset_id": new_dataset_id,
            "previous_dataset_id": dataset_id,
            "row_count_before": result.row_count_before,
            "row_count_after": result.row_count_after,
            "operations_applied": result.applied,
            "duplicate_groups_count": len(result.duplicate_groups),
            "summary_text": self._build_summary_text(result, new_table),
            "preview_rows": self._bounded_preview(result.rows, new_table.columns),
            "wants_workbook": plan.wants_workbook,
        }
        return out

    def register_generated_workbook(
        self,
        dataset_id: str,
        *,
        tenant_id: str,
        owner_id: str = "",
        conversation_id: str = "",
        request_id: str = "",
        tool_id: str = "data.excel_assistant",
        kind: str = "data",
        comparison: dict | None = None,
    ) -> dict:
        """Generate the workbook bytes (existing governed path) and, when an
        ``ArtifactService`` is wired (Block 5.1 artifact integration), register
        it as a canonical, tenant-owned, conversation-attached artifact
        instead of only the internal data_intel blob store."""

        wb = self.generate_excel(dataset_id, tenant_id=tenant_id, kind=kind, comparison=comparison)
        out = {"filename": wb["filename"], "size": wb["size"]}
        if self.artifact_service is None:
            return out
        try:
            rec = self.artifact_service.register_generated(
                tenant_id=tenant_id,
                owner_id=owner_id,
                filename=wb["filename"],
                content=wb["content"],
                mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                conversation_id=conversation_id,
                request_id=request_id,
                tool_id=tool_id,
            )
        except Exception:
            return out
        public = rec.as_public_dict()
        out.update(
            {
                "artifact_id": rec.artifact_id,
                "mime_type": rec.mime_type,
                "view_url": public["view_url"],
                "download_url": public["download_url"],
            }
        )
        return out

    def run_combined_comparison(
        self,
        left_dataset_id: str,
        right_dataset_id: str,
        *,
        tenant_id: str,
    ) -> dict:
        """Block 5.1 Scenario C: compare two workbooks by identifier and
        report BOTH price and stock changes in one pass (chat-facing
        superset of ``run_price_compare_process``/``run_stock_reconcile_process``,
        which each only cover one dimension)."""

        left_desc = self.store.get_dataset(left_dataset_id, tenant_id=tenant_id)
        right_desc = self.store.get_dataset(right_dataset_id, tenant_id=tenant_id)
        if left_desc is None or right_desc is None:
            raise DataIntelError(DATASET_ACCESS_DENIED)
        left_rows = self.store.get_rows(left_dataset_id, tenant_id=tenant_id)
        right_rows = self.store.get_rows(right_dataset_id, tenant_id=tenant_id)
        assert_sync_data_allowed(
            row_count=len(left_rows) + len(right_rows), operations=("compare", "reconcile", "generate_xlsx")
        )
        left_roles = {c.semantic_role for t in left_desc.tables for c in t.columns}
        right_roles = {c.semantic_role for t in right_desc.tables for c in t.columns}
        id_roles = {ROLE_SKU, ROLE_ARTICLE, ROLE_EAN}
        price_roles = {ROLE_PRICE, ROLE_SELLING_PRICE, ROLE_PURCHASE_PRICE}
        stock_roles = {ROLE_STOCK}
        if not (left_roles & id_roles and right_roles & id_roles):
            return {
                "status": "NEEDS_USER_MAPPING",
                "message_safe": "Не удалось однозначно определить столбец-идентификатор (артикул/SKU/EAN) в одной из таблиц.",
            }

        sheets: dict = {}
        summary: dict = {}
        price_result = None
        if left_roles & price_roles and right_roles & price_roles:
            price_result = price_comparison_changed_only(left_rows, right_rows)
            headers = [
                "identifier",
                "product",
                "old_price",
                "new_price",
                "absolute_difference",
                "percentage_difference",
                "match_status",
            ]
            sheets["PRICE_CHANGES"] = {
                "headers": headers,
                "rows": [[c.get(h) for h in headers] for c in price_result["changed"]],
                "text_cols": {0},
            }
            summary.update({f"price_{k}": v for k, v in price_result["summary"].items()})
        stock_result = None
        if left_roles & stock_roles and right_roles & stock_roles:
            stock_result = stock_reconciliation_report(left_rows, right_rows)
            headers = ["identifier", "product", "stock_A", "stock_B", "difference", "status"]
            sheets["STOCK_CHANGES"] = {
                "headers": headers,
                "rows": [[r.get(h) for h in headers] for r in stock_result["rows"]],
                "text_cols": {0},
            }
            summary.update({f"stock_{k}": v for k, v in stock_result["summary"].items()})
        if not sheets:
            return {
                "status": "NEEDS_USER_MAPPING",
                "message_safe": "Не найдены сопоставимые столбцы цены или остатков для сравнения.",
            }
        content = generate_workbook(
            summary=summary,
            sheets=sheets,
            provenance={
                "left_dataset_id": left_dataset_id,
                "right_dataset_id": right_dataset_id,
                "original_preserved": True,
            },
        )
        name = "price_stock_compare_result.xlsx"
        self.store.save_blob(left_dataset_id, name, content, tenant_id=tenant_id)
        lines = []
        if price_result is not None:
            lines.append(f"Изменений цены: {len(price_result['changed'])}.")
        if stock_result is not None:
            changed_stock = [r for r in stock_result["rows"] if str(r.get("status") or "") not in {"", "unchanged", "matched"}]
            lines.append(f"Изменений остатков: {len(changed_stock)}.")
        return {
            "status": "OK",
            "filename": name,
            "content": content,
            "summary": summary,
            "summary_text": " ".join(lines) or "Сравнение выполнено.",
        }

    def quality_report(self, dataset_id: str, *, tenant_id: str) -> dict:
        desc = self.store.get_dataset(dataset_id, tenant_id=tenant_id)
        if desc is None:
            raise DataIntelError(DATASET_ACCESS_DENIED)
        rows = self.store.get_rows(dataset_id, tenant_id=tenant_id)
        cols = desc.tables[0].columns if desc.tables else ()
        return build_quality_report(
            rows=rows,
            columns=cols,
            source_file=desc.provenance.get("filename", "") if isinstance(desc.provenance, dict) else "",
            source_sheet=desc.tables[0].sheet if desc.tables else "",
        )

    def assess_structure(self, dataset_id: str, *, tenant_id: str, required_roles: set[str] | None = None) -> dict:
        return assess_structure_for_process(self, dataset_id, tenant_id=tenant_id, required_roles=required_roles)

    def run_price_compare_process(
        self,
        left_dataset_id: str,
        right_dataset_id: str,
        *,
        tenant_id: str,
        changed_only: bool = True,
    ) -> dict:
        """Offline business workflow: compare two price lists → RESULT/ISSUES/SUMMARY workbook."""
        left_desc = self.store.get_dataset(left_dataset_id, tenant_id=tenant_id)
        right_desc = self.store.get_dataset(right_dataset_id, tenant_id=tenant_id)
        if left_desc is None or right_desc is None:
            raise DataIntelError(DATASET_ACCESS_DENIED)
        id_roles = {ROLE_SKU, ROLE_ARTICLE, ROLE_EAN}
        price_roles = {ROLE_PRICE, ROLE_SELLING_PRICE, ROLE_PURCHASE_PRICE}
        left_roles = {c.semantic_role for t in left_desc.tables for c in t.columns}
        right_roles = {c.semantic_role for t in right_desc.tables for c in t.columns}
        id_ok = bool(left_roles & id_roles) and bool(right_roles & id_roles)
        price_ok = bool(left_roles & price_roles) and bool(right_roles & price_roles)
        if not id_ok or not price_ok:
            return {
                "status": "NEEDS_USER_MAPPING",
                "left": assess_structure_for_process(self, left_dataset_id, tenant_id=tenant_id),
                "right": assess_structure_for_process(self, right_dataset_id, tenant_id=tenant_id),
                "missing": {
                    "left_identifiers": not bool(left_roles & id_roles),
                    "right_identifiers": not bool(right_roles & id_roles),
                    "left_price": not bool(left_roles & price_roles),
                    "right_price": not bool(right_roles & price_roles),
                },
            }
        left_rows = self.store.get_rows(left_dataset_id, tenant_id=tenant_id)
        right_rows = self.store.get_rows(right_dataset_id, tenant_id=tenant_id)
        assert_sync_data_allowed(row_count=len(left_rows) + len(right_rows), operations=("compare", "generate_xlsx"))
        result = price_comparison_changed_only(left_rows, right_rows)
        changed = result["changed"] if changed_only else (
            result["changed"]
            + [
                {
                    "identifier": x.get("key"),
                    "product": x.get("product"),
                    "old_price": x.get("old_price"),
                    "new_price": x.get("new_price"),
                    "absolute_difference": "0",
                    "percentage_difference": "0",
                    "match_status": x.get("match_method"),
                    "sku": x.get("sku"),
                    "ean": x.get("ean"),
                    "warnings": "",
                }
                for x in (result["comparison"].get("matched") or [])
            ]
        )
        headers = [
            "identifier",
            "product",
            "old_price",
            "new_price",
            "absolute_difference",
            "percentage_difference",
            "match_status",
            "sku",
            "ean",
            "warnings",
        ]
        body = [[c.get(h) for h in headers] for c in changed]
        lq = build_quality_report(rows=left_rows, columns=left_desc.tables[0].columns if left_desc.tables else ())
        rq = build_quality_report(rows=right_rows, columns=right_desc.tables[0].columns if right_desc.tables else ())
        issues = list(lq.get("issues") or []) + list(rq.get("issues") or [])
        summary = {
            **result["summary"],
            "changed_only": changed_only,
            "result_rows": len(body),
            "process": "price_compare",
        }
        content = build_business_workbook(
            result_headers=headers,
            result_rows=body,
            issues=issues,
            summary=summary,
            provenance={
                "left_dataset_id": left_dataset_id,
                "right_dataset_id": right_dataset_id,
                "original_preserved": True,
            },
            text_cols={0, 7, 8},
        )
        out_name = "price_compare_result.xlsx"
        self.store.save_blob(left_dataset_id, out_name, content, tenant_id=tenant_id)
        return {
            "status": "OK",
            "filename": out_name,
            "size": len(content),
            "content": content,
            "summary": summary,
            "issues_count": len(issues),
        }

    def run_stock_reconcile_process(self, left_dataset_id: str, right_dataset_id: str, *, tenant_id: str) -> dict:
        if self.store.get_dataset(left_dataset_id, tenant_id=tenant_id) is None:
            raise DataIntelError(DATASET_ACCESS_DENIED)
        if self.store.get_dataset(right_dataset_id, tenant_id=tenant_id) is None:
            raise DataIntelError(DATASET_ACCESS_DENIED)
        left_rows = self.store.get_rows(left_dataset_id, tenant_id=tenant_id)
        right_rows = self.store.get_rows(right_dataset_id, tenant_id=tenant_id)
        report = stock_reconciliation_report(left_rows, right_rows)
        headers = ["identifier", "product", "stock_A", "stock_B", "difference", "status"]
        body = [[r.get(h) for h in headers] for r in report["rows"]]
        content = build_business_workbook(
            result_headers=headers,
            result_rows=body,
            issues=[],
            summary=report["summary"],
            provenance={"left_dataset_id": left_dataset_id, "right_dataset_id": right_dataset_id, "original_preserved": True},
            text_cols={0},
        )
        name = "stock_reconcile_result.xlsx"
        self.store.save_blob(left_dataset_id, name, content, tenant_id=tenant_id)
        return {"status": "OK", "filename": name, "content": content, "summary": report["summary"]}

    def run_merge_dedupe_process(
        self,
        left_dataset_id: str,
        right_dataset_id: str,
        *,
        tenant_id: str,
    ) -> dict:
        left_desc = self.store.get_dataset(left_dataset_id, tenant_id=tenant_id)
        right_desc = self.store.get_dataset(right_dataset_id, tenant_id=tenant_id)
        if left_desc is None or right_desc is None:
            raise DataIntelError(DATASET_ACCESS_DENIED)
        left_rows = self.store.get_rows(left_dataset_id, tenant_id=tenant_id)
        right_rows = self.store.get_rows(right_dataset_id, tenant_id=tenant_id)
        merged = merge_with_provenance(left_rows, right_rows, left_file=left_dataset_id, right_file=right_dataset_id)
        headers = [
            "source_file",
            "source_sheet",
            "source_row",
            "sku",
            "article",
            "ean",
            "product_name",
            "price",
            "stock",
        ]
        body = []
        for r in merged["rows"]:
            body.append(
                [
                    r.get("source_file"),
                    r.get("source_sheet"),
                    r.get("source_row") or r.get("__source_row"),
                    r.get("sku") or r.get("article"),
                    r.get("article"),
                    r.get("ean"),
                    r.get("product_name") or r.get("name"),
                    r.get("price") or r.get("selling_price"),
                    r.get("stock"),
                ]
            )
        conflict_issues = [
            {
                "file": "",
                "sheet": "",
                "row": "",
                "column": c.get("key"),
                "reason": "conflicting_duplicate",
                "severity": "error",
            }
            for c in merged["conflicts"]
        ]
        content = build_business_workbook(
            result_headers=headers,
            result_rows=body,
            issues=conflict_issues,
            summary=merged["summary"],
            provenance={"original_preserved": True, "process": "merge_dedupe"},
            text_cols={0, 1, 3, 4, 5},
        )
        name = "merge_result.xlsx"
        self.store.save_blob(left_dataset_id, name, content, tenant_id=tenant_id)
        return {
            "status": "OK",
            "filename": name,
            "content": content,
            "summary": merged["summary"],
            "conflicts": merged["conflicts"],
        }

    def basic_margin_for_row(self, row: dict) -> dict:
        return basic_margin(row)

    def run_economics_process(
        self,
        dataset_id: str,
        *,
        tenant_id: str,
        policy: EconomicsPolicy | None = None,
        channel: str = "SITE",
        channel_configs: dict | None = None,
    ) -> dict:
        """Block 11 — product economics batch on ingested dataset (reuses Block 10 store)."""
        desc = self.store.get_dataset(dataset_id, tenant_id=tenant_id)
        if desc is None:
            raise DataIntelError(DATASET_ACCESS_DENIED)
        rows = self.store.get_rows(dataset_id, tenant_id=tenant_id)
        assert_sync_data_allowed(row_count=len(rows), operations=("economics", "generate_xlsx"))
        out = run_economics_batch(
            rows,
            policy=policy,
            channel=channel,
            channel_configs=channel_configs,
        )
        name = "economics_result.xlsx"
        self.store.save_blob(dataset_id, name, out["content"], tenant_id=tenant_id)
        out["filename"] = name
        out["dataset_id"] = dataset_id
        self._emit("data.economics_generated", dataset_id=dataset_id, tenant=tenant_id, rows=len(rows))
        return out

    def conflicting_duplicates(self, dataset_id: str, *, tenant_id: str) -> list[dict]:
        rows = self.store.get_rows(dataset_id, tenant_id=tenant_id)
        return find_conflicting_identifier_duplicates(rows)

    def from_acquisition_records(self, records: list, *, tenant_id: str, dataset_id: str | None = None) -> dict:
        """Bridge Acquisition ParsedRecord → dataset rows."""
        rows = []
        for i, rec in enumerate(records):
            fields = dict(getattr(rec, "fields", rec) or {})
            fields["__source_row"] = i + 1
            fields.setdefault("sku", fields.get("supplier_sku"))
            rows.append(fields)
        ds_id = dataset_id or new_id("ds-")
        from data_intel.contracts import ColumnDescriptor, TableDescriptor
        from data_intel.mapping import map_columns

        headers = sorted({k for r in rows for k in r if not str(k).startswith("__")})
        cols = map_columns(headers, rows)
        table = TableDescriptor(
            table_id="acquisition",
            sheet="Acquisition",
            range="A1",
            header_row=1,
            columns=cols,
            row_count=len(rows),
        )
        desc = DatasetDescriptor(
            dataset_id=ds_id,
            tenant_id=tenant_id,
            source_document_id="",
            format="acquisition",
            sheets=("Acquisition",),
            tables=(table,),
            row_count=len(rows),
            column_count=len(headers),
            checksum="",
            provenance={"source": "acquisition"},
        )
        for r in rows:
            r["__row_ref"] = row_ref(ds_id, "acquisition", int(r["__source_row"]))
        self.store.save_dataset(desc, {"acquisition": rows})
        return {"dataset_id": ds_id, "row_count": len(rows)}
