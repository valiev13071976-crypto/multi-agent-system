"""Tool Platform adapter for Data Intelligence."""

from __future__ import annotations

import base64

from data_intel.errors import DataIntelError
from tools.errors import ToolArgumentInvalidError, ToolError, ToolNotFoundError


class DataIntelToolAdapter:
    adapter_id = "data_intel"

    def __init__(self, service=None):
        self._svc = service

    def supports(self, tool_id: str) -> bool:
        return tool_id.startswith("data.")

    def health(self) -> str:
        from tools.models import ADAPTER_HEALTHY, ADAPTER_UNAVAILABLE

        return ADAPTER_HEALTHY if self._svc is not None else ADAPTER_UNAVAILABLE

    def _tenant(self, request) -> str:
        return str(request.tenant_id or "legacy-default")

    def _spreadsheet_refs(self, args: dict) -> list[dict]:
        refs = list(args.get("attachment_refs") or [])
        return [r for r in refs if isinstance(r, dict) and str(r.get("kind") or "") == "spreadsheet"]

    def _ingest_attachment(self, ref: dict, *, tenant: str) -> dict:
        """Fetch trusted attachment bytes via ArtifactService and ingest them
        (Block 5.1 artifact integration: attachment -> authorized source
        artifact -> dataset, never a second/parallel upload path)."""

        artifact_service = getattr(self._svc, "artifact_service", None)
        if artifact_service is None:
            raise ToolArgumentInvalidError()
        rec, blob = artifact_service.get_blob(
            tenant_id=tenant, artifact_id=str(ref.get("artifact_id") or "")
        )
        return self._svc.ingest(
            blob,
            filename=rec.safe_filename or str(ref.get("filename") or "data.xlsx"),
            tenant_id=tenant,
        )

    async def execute_read(self, request, context) -> dict:
        if self._svc is None:
            raise ToolNotFoundError("tool_unavailable")
        args = dict(request.arguments or {})
        op = request.operation
        tenant = self._tenant(request)
        try:
            if op == "ingest":
                raw = args.get("content_b64")
                if not raw:
                    raise ToolArgumentInvalidError()
                data = base64.b64decode(raw)
                row_hint = args.get("row_count")
                if row_hint is not None:
                    from data_intel.planner import assert_sync_data_allowed

                    assert_sync_data_allowed(
                        row_count=int(row_hint),
                        byte_size=len(data),
                        operations=("ingest",),
                    )
                return self._svc.ingest(
                    data,
                    filename=str(args.get("filename") or "data.csv"),
                    tenant_id=tenant,
                    source_document_id=str(args.get("source_document_id") or ""),
                )
            if op == "duplicates":
                ds = str(args.get("dataset_id") or "")
                if not ds:
                    raise ToolArgumentInvalidError()
                return {
                    "groups": self._svc.duplicates(
                        ds,
                        tenant_id=tenant,
                        business_keys=list(args.get("business_keys") or []),
                    )
                }
            if op == "canonical_identity_rows":
                ds = str(args.get("dataset_id") or "")
                if not ds:
                    raise ToolArgumentInvalidError()
                return {"rows": self._svc.canonical_identity_rows(ds, tenant_id=tenant)}
            if op == "merge":
                left = list(args.get("left_rows") or [])
                right = list(args.get("right_rows") or [])
                return self._svc.merge(
                    left,
                    right,
                    keys=list(args.get("keys") or []),
                    how=str(args.get("how") or "inner"),
                )
            if op == "profile":
                ds = str(args.get("dataset_id") or "")
                if not ds:
                    raise ToolArgumentInvalidError()
                return self._svc.profile(ds, tenant_id=tenant)
            if op == "normalize":
                ds = str(args.get("dataset_id") or "")
                if not ds:
                    raise ToolArgumentInvalidError()
                return self._svc.normalize(ds, tenant_id=tenant)
            if op == "search":
                ds = str(args.get("dataset_id") or "")
                if not ds:
                    raise ToolArgumentInvalidError()
                return self._svc.search(
                    ds,
                    tenant_id=tenant,
                    inn=args.get("inn"),
                    company_name=args.get("company_name"),
                    sku=args.get("sku"),
                    ean=args.get("ean"),
                    article=args.get("article"),
                    document_number=args.get("document_number"),
                    amount=args.get("amount"),
                    date=args.get("date"),
                    filters=args.get("filters"),
                    fuzzy_name=bool(args.get("fuzzy_name")),
                    sort_by=args.get("sort_by"),
                    sort_desc=bool(args.get("sort_desc")),
                    offset=int(args.get("offset") or 0),
                    limit=int(args.get("limit") or 100),
                )
            if op == "match":
                return self._svc.match(
                    dict(args.get("left") or {}),
                    dict(args.get("right") or {}),
                    entity_type=str(args.get("entity_type") or "counterparty"),
                )
            if op == "compare":
                return self._svc.compare_prices(
                    list(args.get("left_rows") or []),
                    list(args.get("right_rows") or []),
                    left_supplier=str(args.get("left_supplier") or "left"),
                    right_supplier=str(args.get("right_supplier") or "right"),
                )
            if op == "reconcile":
                return self._svc.reconcile(
                    str(args.get("kind") or "payment"),
                    list(args.get("left_rows") or []),
                    list(args.get("right_rows") or []),
                )
            if op == "aggregate":
                ds = str(args.get("dataset_id") or "")
                if not ds:
                    raise ToolArgumentInvalidError()
                return {
                    "rows": self._svc.aggregate(
                        ds,
                        tenant_id=tenant,
                        group_by=list(args.get("group_by") or []),
                        measures=dict(args.get("measures") or {"_rows": "count"}),
                    )
                }
            if op == "generate_excel":
                ds = str(args.get("dataset_id") or "")
                if not ds:
                    raise ToolArgumentInvalidError()
                result = self._svc.generate_excel(
                    ds,
                    tenant_id=tenant,
                    kind=str(args.get("kind") or "data"),
                    comparison=args.get("comparison"),
                )
                content = result.pop("content", b"")
                return {
                    **{k: v for k, v in result.items() if k != "content"},
                    "content_b64": base64.b64encode(content).decode("ascii") if content else "",
                }
            if op == "assist":
                # CANONICAL TABLE EXECUTION: ``use_model_plan`` is a plain
                # boolean flag (JSON-serializable, no behavior change to the
                # ``arguments`` schema otherwise) set only by
                # ``business_assistant.conversation_gateway._maybe_execute_
                # canonical_table_operation`` -- every other/legacy caller of
                # this SAME "assist" operation is completely unaffected and
                # keeps going through ``_assist``/``compile_request`` exactly
                # as before.
                if args.get("use_model_plan"):
                    return await self._assist_structured(args, request, tenant)
                return self._assist(args, request, tenant)
            if op == "compare_workbooks":
                return self._compare_workbooks(args, request, tenant)
            raise ToolArgumentInvalidError()
        except DataIntelError as exc:
            raise ToolError(exc.reason) from exc

    def _assist(self, args: dict, request, tenant: str) -> dict:
        """Block 5.1 chat-facing entry point: ingest a newly attached
        spreadsheet if present (or continue an existing ``dataset_id``),
        compile the free-text ``text`` into a bounded operation plan, apply
        it, and optionally register a generated workbook artifact."""

        text = str(args.get("text") or "")
        dataset_id = str(args.get("dataset_id") or "")
        sheets = self._spreadsheet_refs(args)
        ingest_tables = None
        if sheets:
            ingest_result = self._ingest_attachment(sheets[0], tenant=tenant)
            if ingest_result.get("async"):
                return {
                    "status": "BATCH_QUEUED",
                    "dataset_id": ingest_result["dataset_id"],
                    "workflow_id": ingest_result.get("workflow_id"),
                    "summary_text": "Файл большой — обрабатываю в фоне и пришлю результат отдельно.",
                }
            dataset_id = ingest_result["dataset_id"]
            ingest_tables = ingest_result.get("tables")
        if not dataset_id:
            raise ToolArgumentInvalidError()

        result = self._svc.execute_nl_request(
            dataset_id,
            text,
            tenant_id=tenant,
            current_selection=args.get("current_selection"),
        )
        if result.get("status") == "OK" and result.get("wants_workbook"):
            # Production defect closure (downloadable Excel): the user
            # explicitly asked to see/download the RESULT of a table
            # operation -- render it as a normal, user-facing business
            # workbook (``kind="business_result"``, see
            # ``DataIntelligenceService.generate_excel``), never the
            # internal debug-oriented ``SUMMARY``/``ISSUES``/``Provenance``
            # export ``kind="data"`` (still available, unchanged, for any
            # caller that explicitly asks for it via the standalone
            # ``generate_excel`` tool operation).
            reg = self._svc.register_generated_workbook(
                result["dataset_id"],
                tenant_id=tenant,
                owner_id=str(getattr(request, "user_id", "") or ""),
                conversation_id=str(args.get("conversation_id") or ""),
                request_id=str(getattr(request, "request_id", "") or ""),
                kind="business_result",
            )
            result["workbook"] = reg
        if ingest_tables is not None:
            result["ingest_tables"] = ingest_tables
        return result

    async def _assist_structured(self, args: dict, request, tenant: str) -> dict:
        """CANONICAL TABLE EXECUTION: the SAME entry point as ``_assist``
        (same tool_id/operation, same ``dataset_id``/``text`` arguments),
        but the request is interpreted by ONE model call
        (``data_intel.nl_plan_llm.compile_request_via_model``) into a
        validated structured plan instead of ``compile_request``'s bounded
        regex/stem grammar -- see ``DataIntelligenceService.
        execute_structured_plan_via_model``. No attachment ingest here:
        by the time a canonical-table-execution turn reaches this branch
        the Workset already owns an attached dataset (PR #91); a fresh
        upload always goes through the existing ``_assist`` path first."""

        text = str(args.get("text") or "")
        dataset_id = str(args.get("dataset_id") or "")
        if not dataset_id:
            raise ToolArgumentInvalidError()

        result = await self._svc.execute_structured_plan_via_model(
            dataset_id,
            text,
            tenant_id=tenant,
            selected_identifiers=tuple(str(x) for x in (args.get("selected_identifiers") or ())),
        )
        if result.get("status") == "OK" and result.get("wants_workbook"):
            # Production defect closure (downloadable Excel) -- see the
            # SAME comment in ``_assist`` above.
            reg = self._svc.register_generated_workbook(
                result["dataset_id"],
                tenant_id=tenant,
                owner_id=str(getattr(request, "user_id", "") or ""),
                conversation_id=str(args.get("conversation_id") or ""),
                request_id=str(getattr(request, "request_id", "") or ""),
                kind="business_result",
            )
            result["workbook"] = reg
        return result

    def _compare_workbooks(self, args: dict, request, tenant: str) -> dict:
        """Block 5.1 Scenario C: ingest two attached spreadsheets and produce
        a combined price/stock reconciliation report + workbook."""

        sheets = self._spreadsheet_refs(args)
        if len(sheets) < 2:
            raise ToolArgumentInvalidError()
        left = self._ingest_attachment(sheets[0], tenant=tenant)
        right = self._ingest_attachment(sheets[1], tenant=tenant)
        if left.get("async") or right.get("async"):
            return {
                "status": "BATCH_QUEUED",
                "summary_text": "Файлы большие — сравнение выполняется в фоне.",
            }
        result = self._svc.run_combined_comparison(
            left["dataset_id"], right["dataset_id"], tenant_id=tenant
        )
        if result.get("status") == "OK":
            content = result.pop("content", b"")
            artifact_service = getattr(self._svc, "artifact_service", None)
            if artifact_service is not None and content:
                try:
                    rec = artifact_service.register_generated(
                        tenant_id=tenant,
                        owner_id=str(getattr(request, "user_id", "") or ""),
                        filename=str(result.get("filename") or "compare_result.xlsx"),
                        content=content,
                        mime_type=(
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                        ),
                        conversation_id=str(args.get("conversation_id") or ""),
                        request_id=str(getattr(request, "request_id", "") or ""),
                        tool_id="data.compare_workbooks",
                    )
                    public = rec.as_public_dict()
                    result["workbook"] = {
                        "artifact_id": rec.artifact_id,
                        "filename": rec.safe_filename,
                        "mime_type": rec.mime_type,
                        "view_url": public["view_url"],
                        "download_url": public["download_url"],
                    }
                except Exception:
                    pass
        return result
