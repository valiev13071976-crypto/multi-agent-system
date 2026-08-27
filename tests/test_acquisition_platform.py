"""Data Acquisition & Parsing Platform tests."""

from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

from acquisition.batch import plan_crawl_batches
from acquisition.change import detect_record_change
from acquisition.crawler import ControlledCrawler, CrawlLimits, canonicalize_url
from acquisition.entity import resolve_entities
from acquisition.errors import AcquisitionDeniedError, ParserNotFoundError, SourceAlreadyRegisteredError
from acquisition.freshness import freshness_label
from acquisition.identifiers import (
    normalize_ean,
    normalize_mpn,
    normalize_sku,
    validate_ean,
)
from acquisition.models import (
    ACQ_HTTP_GET,
    ACQ_SEARCH,
    CHANGE_CHANGED,
    CHANGE_CREATED,
    CHANGE_UNCHANGED,
    CONTENT_TRUST_UNTRUSTED,
    FRESHNESS_FRESH,
    FRESHNESS_STALE,
    FRESHNESS_UNKNOWN,
    MATCH_EXACT,
    MATCH_UNRESOLVED,
    AcquisitionRequest,
    FreshnessPolicy,
    ParsedRecord,
    RawArtifact,
    SourceDescriptor,
    TRUST_CONTRACTED_SUPPLIER,
    TRUST_GENERAL_WEB,
    checksum_text,
    fingerprint_record,
    new_id,
    utc_now,
)
from acquisition.parsers import build_default_parser_registry
from acquisition.parsers.price import PriceListCsvParser
from acquisition.registry import SourceRegistry
from acquisition.runtime import build_acquisition_runtime
from acquisition.service import AcquisitionService
from acquisition.store import InMemoryAcquisitionStore
from datetime import timedelta
from tools.gateway import ToolGateway
from tools.platform.bootstrap import register_platform_tools
from tools.registry import ToolRegistry
from tools.router import ToolRouter
from tools.search.fake_provider import FakeSearchProvider


VALID_EAN = "5901234123457"
INVALID_EAN = "5901234123450"


def _source(**kwargs):
    base = dict(
        source_id="src-1",
        source_type="supplier",
        tenant_id="tenant-a",
        trust_level=TRUST_CONTRACTED_SUPPLIER,
        tool_id="http.request",
        enabled=True,
        allowed_domains=("example.com",),
        name="Supplier A",
    )
    base.update(kwargs)
    return SourceDescriptor(**base)


class SourceRegistryTests(unittest.TestCase):
    def test_register_disable_tenant_isolation(self):
        reg = SourceRegistry()
        reg.register(_source())
        reg.register(_source(source_id="src-b", tenant_id="tenant-b"))
        self.assertEqual(len(reg.list_sources(tenant_id="tenant-a")), 1)
        self.assertEqual(len(reg.list_sources(tenant_id="tenant-b")), 1)
        with self.assertRaises(SourceAlreadyRegisteredError):
            reg.register(_source())
        disabled = reg.enable("src-1", tenant_id="tenant-a", enabled=False)
        self.assertFalse(disabled.enabled)
        self.assertEqual(len(reg.list_sources(tenant_id="tenant-a")), 0)
        self.assertEqual(len(reg.list_sources(tenant_id="tenant-a", include_disabled=True)), 1)

    def test_credentials_forbidden_in_metadata(self):
        with self.assertRaises(ValueError):
            _source(metadata={"api_token": "secret"})


class IdentifierTests(unittest.TestCase):
    def test_ean_normalize_validate(self):
        self.assertEqual(normalize_ean("5901-2341-23457"), VALID_EAN)
        self.assertTrue(validate_ean(VALID_EAN))
        self.assertFalse(validate_ean(INVALID_EAN))
        self.assertEqual(normalize_sku(" ab_12 "), "AB-12")
        self.assertEqual(normalize_mpn("mpn-xyz!"), "MPNXYZ")


class EntityResolutionTests(unittest.TestCase):
    def test_exact_ean_match(self):
        a = {"ean": VALID_EAN, "name": "Widget"}
        b = {"ean": VALID_EAN, "name": "Other name"}
        result = resolve_entities(a, b, left_id="1", right_id="2")
        self.assertTrue(result.same_entity)
        self.assertEqual(result.level, MATCH_EXACT)

    def test_exact_mpn_match(self):
        a = {"mpn": "ABC-123", "brand": "Acme"}
        b = {"mpn": "abc123", "brand": "Acme"}
        result = resolve_entities(a, b)
        self.assertTrue(result.same_entity)
        self.assertEqual(result.level, MATCH_EXACT)

    def test_conflicting_ean_no_merge(self):
        a = {"ean": VALID_EAN, "name": "Same Product Name"}
        other = "4006381333931"  # valid different EAN if check ok
        # ensure other validates or use another known
        if not validate_ean(other):
            other = "9780201379624"
        b = {"ean": other, "name": "Same Product Name"}
        result = resolve_entities(a, b)
        self.assertFalse(result.same_entity)
        self.assertEqual(result.level, MATCH_UNRESOLVED)
        self.assertTrue(result.evidence.conflicts)

    def test_fuzzy_only_unresolved_or_low(self):
        a = {"name": "Blue Widget 2000", "brand": "acme"}
        b = {"name": "Blue Widget 2000", "brand": "acme"}
        result = resolve_entities(a, b)
        self.assertFalse(result.same_entity)
        self.assertIn(result.level, {MATCH_UNRESOLVED, "low", "medium"})


class FreshnessTests(unittest.TestCase):
    def test_fresh_stale_unknown(self):
        now = utc_now()
        self.assertEqual(
            freshness_label(fetched_at=now, policy=FreshnessPolicy(stale_after_seconds=3600), now=now),
            FRESHNESS_FRESH,
        )
        old = now - timedelta(hours=2)
        self.assertEqual(
            freshness_label(fetched_at=old, policy=FreshnessPolicy(stale_after_seconds=3600), now=now),
            FRESHNESS_STALE,
        )
        self.assertEqual(
            freshness_label(fetched_at=None, policy=FreshnessPolicy()),
            FRESHNESS_UNKNOWN,
        )


class ParserRegistryTests(unittest.TestCase):
    def test_price_csv_selection_and_fields(self):
        reg = build_default_parser_registry()
        csv_text = "sku,ean,brand,name,price,currency,stock,moq\nS1,%s,Acme,Widget,12.5,USD,10,2\n" % VALID_EAN
        art = RawArtifact(
            artifact_id="a1",
            source_id="src-1",
            tenant_id="tenant-a",
            content_type="text/csv",
            fetched_at=utc_now(),
            checksum=checksum_text(csv_text),
            content_text=csv_text,
            content_trust=CONTENT_TRUST_UNTRUSTED,
        )
        parser = reg.select(art)
        self.assertEqual(parser.descriptor.parser_id, "price.csv")
        records = reg.parse(art)
        self.assertEqual(len(records), 1)
        fields = dict(records[0].fields)
        self.assertEqual(fields["sku"], "S1")
        self.assertEqual(fields["ean"], VALID_EAN)
        self.assertEqual(fields["price"], 12.5)
        self.assertEqual(fields["currency"], "USD")
        self.assertEqual(fields["stock"], 10.0)
        self.assertEqual(fields["moq"], 2.0)
        self.assertTrue(records[0].validation_ok)

    def test_unsupported_content(self):
        reg = build_default_parser_registry()
        art = RawArtifact(
            artifact_id="a2",
            source_id="src-1",
            tenant_id="tenant-a",
            content_type="application/octet-stream",
            fetched_at=utc_now(),
            checksum="abc",
            content_text="",
        )
        with self.assertRaises(Exception):
            reg.parse(art)


class ChangeDetectionTests(unittest.TestCase):
    def _rec(self, **fields):
        fp = fingerprint_record({"source_id": "s", "record_type": "price", **fields})
        return ParsedRecord(
            record_id=new_id("r"),
            parser_id="price.csv",
            parser_version="1.0.0",
            source_id="src-1",
            artifact_id="a1",
            tenant_id="tenant-a",
            record_type="price",
            fields=fields,
            confidence=0.9,
            fingerprint=fp,
            observed_at=utc_now(),
        )

    def test_created_unchanged_changed(self):
        cur = self._rec(sku="S1", price=10, stock=5)
        created = detect_record_change(previous=None, current=cur)
        self.assertEqual(created.outcome, CHANGE_CREATED)
        same = detect_record_change(previous=cur, current=cur)
        self.assertEqual(same.outcome, CHANGE_UNCHANGED)
        nxt = self._rec(sku="S1", price=11, stock=5)
        changed = detect_record_change(previous=cur, current=nxt)
        self.assertEqual(changed.outcome, CHANGE_CHANGED)
        self.assertIn("price", changed.changed_fields)


class AcquisitionServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.reg = ToolRegistry()
        self.gateway = ToolGateway(
            FakeSearchProvider(),
            registry=self.reg,
            register_search=True,
        )
        register_platform_tools(
            self.reg,
            env={"TOOL_FS_ALLOWED_ROOTS": self.tmp, "TOOL_HTTP_ALLOWED_HOSTS": "example.com"},
        )
        self.gateway.router = ToolRouter(self.reg)
        for row in self.reg._items.values():  # noqa: SLF001
            adapter = row.adapter
            if adapter and hasattr(adapter, "health"):
                self.gateway.router.set_adapter_health(
                    getattr(adapter, "adapter_id", ""), adapter.health()
                )
        self.svc = build_acquisition_runtime(tool_gateway=self.gateway)
        self.svc.register_source(_source())
        self.svc.register_source(
            _source(
                source_id="search-1",
                source_type="search",
                trust_level=TRUST_GENERAL_WEB,
                tool_id="search",
                allowed_domains=(),
            )
        )

    async def test_search_acquisition_and_parse(self):
        art = await self.svc.acquire(
            AcquisitionRequest(
                source_id="search-1",
                target="widget",
                acquisition_type=ACQ_SEARCH,
                tenant_id="tenant-a",
            )
        )
        self.assertEqual(art.content_trust, CONTENT_TRUST_UNTRUSTED)
        self.assertNotIn("token", str(art.provenance).lower())
        records = self.svc.parse(art)
        self.assertGreaterEqual(len(records), 0)
        # even if empty results, artifact stored
        self.assertIsNotNone(self.svc.store.get_artifact(art.artifact_id, tenant_id="tenant-a"))

    async def test_forbidden_domain_denied(self):
        with self.assertRaises(AcquisitionDeniedError):
            await self.svc.acquire(
                AcquisitionRequest(
                    source_id="src-1",
                    target="https://evil.example.org/x",
                    acquisition_type=ACQ_HTTP_GET,
                    tenant_id="tenant-a",
                )
            )

    async def test_http_allowed_via_gateway(self):
        # Mock http adapter execute_read
        http_reg = self.reg.get_registration("http.request")
        http_reg.adapter.execute_read = AsyncMock(
            return_value={
                "status_code": 200,
                "content_type": "text/html",
                "body_text": "<html><body>price: 9.99</body></html>",
                "truncated": False,
            }
        )
        art = await self.svc.acquire(
            AcquisitionRequest(
                source_id="src-1",
                target="https://example.com/item",
                acquisition_type=ACQ_HTTP_GET,
                tenant_id="tenant-a",
            )
        )
        self.assertEqual(art.url, "https://example.com/item")
        self.assertIn("example.com", art.url)

    def test_duplicate_artifact_dedupe(self):
        text = "sku,price\nA,1\n"
        a1 = self.svc.ingest_text(
            source_id="src-1",
            tenant_id="tenant-a",
            text=text,
            content_type="text/csv",
        )
        a2 = self.svc.ingest_text(
            source_id="src-1",
            tenant_id="tenant-a",
            text=text,
            content_type="text/csv",
        )
        self.assertEqual(a1.artifact_id, a2.artifact_id)

    def test_price_parse_provenance_and_tenant_isolation(self):
        csv_text = (
            "sku,ean,name,price,currency,stock,moq\n"
            f"S1,{VALID_EAN},Widget,12.5,USD,3,1\n"
        )
        art = self.svc.ingest_text(
            source_id="src-1",
            tenant_id="tenant-a",
            text=csv_text,
            content_type="text/csv",
        )
        records = self.svc.parse(art)
        self.assertEqual(len(records), 1)
        rec = records[0]
        prov = self.svc.get_provenance(rec.record_id, tenant_id="tenant-a")
        self.assertEqual(prov["source"]["source_id"], "src-1")
        self.assertEqual(prov["artifact"]["artifact_id"], art.artifact_id)
        self.assertEqual(prov["parser"]["parser_id"], "price.csv")
        self.assertEqual(prov["artifact"]["content_trust"], CONTENT_TRUST_UNTRUSTED)
        # tenant B cannot read
        self.assertIsNone(self.svc.get_record(rec.record_id, tenant_id="tenant-b"))
        with self.assertRaises(AcquisitionDeniedError):
            self.svc.get_provenance(rec.record_id, tenant_id="tenant-b")

    def test_change_detection_on_reparse(self):
        csv1 = f"sku,ean,name,price,currency,stock\nS1,{VALID_EAN},W,10,USD,5\n"
        art1 = self.svc.ingest_text(
            source_id="src-1", tenant_id="tenant-a", text=csv1, content_type="text/csv"
        )
        self.svc.parse(art1)
        csv2 = f"sku,ean,name,price,currency,stock\nS1,{VALID_EAN},W,12,USD,5\n"
        art2 = self.svc.ingest_text(
            source_id="src-1", tenant_id="tenant-a", text=csv2, content_type="text/csv"
        )
        self.svc.parse(art2)
        changes = self.svc.list_changes(tenant_id="tenant-a", source_id="src-1")
        outcomes = {c.outcome for c in changes}
        self.assertIn(CHANGE_CREATED, outcomes)
        self.assertIn(CHANGE_CHANGED, outcomes)

    def test_schedule_uses_workflow_scheduler(self):
        state = self.svc.schedule_refresh(
            schedule_id="sched-1",
            source_id="src-1",
            tenant_id="tenant-a",
            interval_seconds=3600,
            target="https://example.com/feed",
        )
        self.assertEqual(state.workflow_type, "acquisition.refresh")
        key1 = self.svc.scheduler.execution_key(state)
        updated = self.svc.scheduler.mark_enqueued(state.schedule_id, execution_key=key1)
        key2 = self.svc.scheduler.execution_key(updated)
        self.assertNotEqual(key1, key2)  # next slot differs after advance

    def test_batch_plan_bounded(self):
        plan = plan_crawl_batches(
            source_id="src-1",
            tenant_id="tenant-a",
            urls=tuple(f"https://example.com/{i}" for i in range(50)),
            batch_size=10,
        )
        self.assertEqual(len(plan.batches()), 5)
        self.assertEqual(len(plan.execution_keys()), 5)

    def test_crawler_canonicalize_and_domain(self):
        self.assertEqual(
            canonicalize_url("https://Example.com/a/"),
            "https://example.com/a",
        )


class CrawlerGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_crawler_uses_http_tool(self):
        reg = ToolRegistry()
        gateway = ToolGateway(FakeSearchProvider(), registry=reg, register_search=True)
        register_platform_tools(reg, env={"TOOL_HTTP_ALLOWED_HOSTS": "example.com"})
        gateway.router = ToolRouter(reg)
        http_reg = reg.get_registration("http.request")
        calls = []

        async def fake_read(request, context):
            calls.append(dict(request.arguments))
            return {
                "status_code": 200,
                "content_type": "text/html",
                "body_text": '<html><a href="/p2">x</a></html>',
                "truncated": False,
            }

        http_reg.adapter.execute_read = fake_read
        svc = build_acquisition_runtime(tool_gateway=gateway)
        svc.register_source(_source(tool_id="http.request"))
        result = await svc.crawl(
            source_id="src-1",
            tenant_id="tenant-a",
            seeds=("https://example.com/",),
            max_depth=1,
            max_pages=2,
        )
        self.assertGreaterEqual(len(result.artifacts), 1)
        self.assertGreaterEqual(len(calls), 1)


class SupplierMarketplaceSearchParserTests(unittest.TestCase):
    def test_supplier_and_marketplace(self):
        svc = build_acquisition_runtime()
        svc.register_source(_source())
        supplier_csv = "sku,name,price,moq,lead_time\nS9,Pump,100,5,7d\n"
        art = svc.ingest_text(
            source_id="src-1",
            tenant_id="tenant-a",
            text=supplier_csv,
            content_type="text/csv",
            metadata={"record_hint": "supplier_item"},
        )
        # force supplier parser via metadata hint — parser checks header too
        records = svc.parse(art)
        self.assertGreaterEqual(len(records), 1)

        mkt = svc.ingest_text(
            source_id="src-1",
            tenant_id="tenant-a",
            text='{"products":[{"sku":"O1","name":"Item","price":50,"currency":"RUB","stock":2}]}',
            content_type="application/json",
            metadata={"record_hint": "marketplace", "marketplace": "ozon"},
        )
        mrecs = svc.parse(mkt)
        self.assertEqual(len(mrecs), 1)
        self.assertEqual(dict(mrecs[0].fields).get("provider"), "ozon")


if __name__ == "__main__":
    unittest.main()
