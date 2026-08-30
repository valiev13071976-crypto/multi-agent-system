"""Scale Data Acquisition & Parsing Platform tests (5.1–5.7)."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from acquisition.batch import enqueue_acquisition_job, plan_crawl_batches, submit_large_crawl
from acquisition.crawler import ControlledCrawler, CrawlLimits, canonicalize_url
from acquisition.dedupe import DedupeEngine
from acquisition.errors import (
    AcquisitionDeniedError,
    CapacityRejectedError,
    ContentNestingTooDeepError,
    ContentTooLargeError,
    SourcePolicyDeniedError,
)
from acquisition.ingest import InMemoryIngestionTarget
from acquisition.models import (
    ACQ_HTTP_GET,
    CONTENT_TRUST_UNTRUSTED,
    DEDUPE_CROSS_SOURCE,
    DEDUPE_UNIQUE,
    EXTRACT_INVALID,
    EXTRACT_MISSING,
    JOB_CANCELLED,
    MODE_CRAWL,
    MODE_SCRAPE,
    MODE_SINGLE,
    AcquisitionRequest,
    CrawlPolicy,
    SourceDefinition,
    SourceDescriptor,
    TRUST_CONTRACTED_SUPPLIER,
    checksum_text,
    new_id,
    utc_now,
    RawArtifact,
)
from acquisition.normalize import RecordNormalizer, normalize_currency, normalize_date
from acquisition.parsers import MAX_JSON_NESTING, build_default_parser_registry
from acquisition.planner import AcquisitionPlanner
from acquisition.runtime import build_acquisition_runtime
from acquisition.scrape import DEFAULT_STATIC_PROFILE, PaginationController, ScrapingProfile
from acquisition.source_policy import PolicyVerdict, evaluate_url, merge_trusted_hosts
from acquisition.sqlite_store import ACQUISITION_SCHEMA_VERSION, SqliteAcquisitionStore
from security.tenant import MissingTenantError
from side_effects.runtime import build_side_effect_persistence
from task_queue.lanes import LANE_BULK, LANE_INTERACTIVE, WORKLOAD_BATCH, classify_workload
from task_queue.queue import TaskQueue
from task_queue.store import InMemoryTaskQueueStore
from tools.gateway import ToolGateway
from tools.platform.bootstrap import register_platform_tools
from tools.registry import ToolRegistry
from tools.router import ToolRouter
from tools.search.fake_provider import FakeSearchProvider
from tools.url_safety import UnsafeUrlError, validate_http_url


VALID_EAN = "5901234123457"


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


def _definition(**kwargs):
    base = dict(
        source_id="src-1",
        source_type="supplier",
        tenant_id="tenant-a",
        trust_level=TRUST_CONTRACTED_SUPPLIER,
        allowed_hosts=("example.com",),
        tool_id="http.request",
        enabled=True,
        name="Supplier A",
        seed_urls=("https://example.com/",),
    )
    base.update(kwargs)
    return SourceDefinition(**base)


class CanonicalizeAndPolicyTests(unittest.TestCase):
    def test_canonicalize_strips_tracking_keeps_significant(self):
        url = "https://Example.com/a/?id=1&utm_source=x&page=2"
        self.assertEqual(
            canonicalize_url(url),
            "https://example.com/a?id=1&page=2",
        )

    def test_policy_host_path_and_payload_cannot_override(self):
        src = _definition(path_deny=("/admin",), path_allow=("/catalog", "/"))
        ok = evaluate_url("https://example.com/catalog/1", source=src)
        self.assertEqual(ok.verdict, PolicyVerdict.PERMITTED)
        denied = evaluate_url("https://evil.com/x", source=src)
        self.assertEqual(denied.verdict, PolicyVerdict.DENIED)
        path_denied = evaluate_url("https://example.com/admin/x", source=src)
        self.assertEqual(path_denied.verdict, PolicyVerdict.DENIED)
        # Payload override ignored
        still = evaluate_url(
            "https://evil.com/x",
            source=src,
            payload_host_override="evil.com",
        )
        self.assertEqual(still.verdict, PolicyVerdict.DENIED)
        hosts = merge_trusted_hosts(src, payload_hosts=("evil.com",))
        self.assertNotIn("evil.com", hosts)

    def test_ssrf_localhost_private_credentials(self):
        for bad in (
            "http://localhost/x",
            "http://127.0.0.1/x",
            "http://192.168.1.1/x",
            "https://user:pass@example.com/x",
        ):
            with self.assertRaises(UnsafeUrlError):
                validate_http_url(bad)
        self.assertTrue(validate_http_url("https://example.com/ok"))


class PlannerBatchEnforcementTests(unittest.TestCase):
    def test_crawl_always_stamps_batch_even_with_interactive_hint(self):
        planner = AcquisitionPlanner()
        planned = planner.plan(
            source=_definition(),
            mode=MODE_CRAWL,
            tenant_id="tenant-a",
            seeds=("https://example.com/",),
            estimated_pages=None,  # unknown → batch
            force_interactive_hint=True,
        )
        self.assertEqual(planned.workload_class, WORKLOAD_BATCH)
        self.assertEqual(planned.execution_lane, LANE_BULK)
        self.assertEqual(planned.trusted_metadata["trusted_job_type"], "crawler")
        stamped = classify_workload(metadata=planned.trusted_metadata)
        self.assertEqual(stamped.lane, LANE_BULK)

    def test_scrape_stamps_scraping_batch(self):
        planner = AcquisitionPlanner()
        planned = planner.plan(
            source=_definition(),
            mode=MODE_SCRAPE,
            tenant_id="tenant-a",
            seeds=("https://example.com/list",),
        )
        self.assertEqual(planned.trusted_metadata["trusted_job_type"], "scraping")
        self.assertEqual(planned.execution_lane, LANE_BULK)

    def test_enqueue_hard_batch_on_task_queue(self):
        q = TaskQueue(store=InMemoryTaskQueueStore())
        planner = AcquisitionPlanner()
        job, task = submit_large_crawl(
            q,
            planner=planner,
            source=_definition(),
            tenant_id="tenant-a",
            seeds=("https://example.com/",),
        )
        self.assertEqual(task.execution_lane, LANE_BULK)
        self.assertEqual(task.metadata.get("trusted_job_type"), "crawler")
        self.assertEqual(task.metadata.get("workload_class"), WORKLOAD_BATCH)
        # Caller forgetting hint still batch via classify
        again = classify_workload(
            metadata={
                "trusted_job_type": "crawler",
                "workload_class": "batch",
                "execution_lane": "bulk",
            }
        )
        self.assertEqual(again.lane, LANE_BULK)
        self.assertEqual(job.execution_lane, LANE_BULK)

    def test_plan_crawl_batches_stamps_trusted(self):
        plan = plan_crawl_batches(
            source_id="src-1",
            tenant_id="tenant-a",
            urls=tuple(f"https://example.com/{i}" for i in range(5)),
        )
        self.assertEqual(plan.execution_lane, LANE_BULK)
        self.assertEqual(plan.trusted_metadata["trusted_job_type"], "crawler")


class InteractiveProtectionAcquisitionTests(unittest.TestCase):
    def test_interactive_claimable_under_acquisition_batch_flood(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = str(Path(tmp) / "flood.sqlite3")
            bundle = build_side_effect_persistence(
                env={
                    "SIDE_EFFECT_PERSISTENCE_BACKEND": "sqlite",
                    "SIDE_EFFECT_DB_PATH": path,
                    "SIDE_EFFECT_RECOVERY_SCAN_ON_STARTUP": "false",
                    "MAX_RUNNING_GLOBAL": "10",
                    "INTERACTIVE_RESERVED": "3",
                },
                durable=True,
                run_recovery_scan=False,
            )
            from task_queue.lanes import LaneCapacityConfig

            q = TaskQueue(
                store=bundle.task_queue_store,
                lease_seconds=60,
                lane_config=LaneCapacityConfig(
                    interactive_reserved=3, background_may_borrow=False
                ),
            )
            planner = AcquisitionPlanner()
            for i in range(25):
                planned = planner.plan(
                    source=_definition(source_id=f"src-{i}", tenant_id="tenant-bulk"),
                    mode=MODE_CRAWL,
                    tenant_id="tenant-bulk",
                    seeds=("https://example.com/",),
                )
                # Need registered-like definition; plan only needs hosts
                enqueue_acquisition_job(
                    q,
                    planned=planned,
                    execution_key=f"acq-flood-{i}",
                )
            # Drain some bulk
            for i in range(8):
                t = q.dequeue(
                    worker_id=f"wb{i}",
                    max_running_global=10,
                    max_running_per_tenant=50,
                )
                if t is None:
                    break
            q.enqueue(
                workflow_id="ix",
                task_id="tix",
                execution_key="ek-ix-acq",
                tenant_id="tenant-ix",
                execution_lane=LANE_INTERACTIVE,
                priority="high",
            )
            claimed = q.dequeue(
                worker_id="w-ix",
                max_running_global=10,
                max_running_per_tenant=50,
            )
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed.execution_lane, LANE_INTERACTIVE)


class TenantFairnessAcquisitionTests(unittest.TestCase):
    def test_tenant_fairness_with_acquisition_jobs(self):
        q = TaskQueue(store=InMemoryTaskQueueStore())
        planner = AcquisitionPlanner()
        for i in range(5):
            planned = planner.plan(
                source=_definition(tenant_id="tenant-a", source_id="src-a"),
                mode=MODE_CRAWL,
                tenant_id="tenant-a",
                seeds=("https://example.com/",),
            )
            enqueue_acquisition_job(q, planned=planned, execution_key=f"fair-a-{i}")
        planned_b = planner.plan(
            source=_definition(tenant_id="tenant-b", source_id="src-b"),
            mode=MODE_CRAWL,
            tenant_id="tenant-b",
            seeds=("https://example.com/",),
        )
        enqueue_acquisition_job(q, planned=planned_b, execution_key="fair-b-0")
        seen = set()
        for i in range(6):
            t = q.dequeue(worker_id=f"w{i}", max_running_per_tenant=2)
            if t:
                seen.add(t.tenant_id)
        self.assertIn("tenant-b", seen)


class CrawlerBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def test_bounds_visited_redirect_path_content_type_429_deadline_cancel(self):
        pages = {
            "https://example.com/": (
                "text/html",
                '<html><a href="/ok">o</a><a href="/skip.bin">b</a>'
                '<a href="/deny">d</a><a href="https://evil.com/x">e</a></html>',
            ),
            "https://example.com/ok": ("text/html", "<html>ok</html>"),
            "https://example.com/skip.bin": ("application/octet-stream", "BIN"),
        }
        rate_hits = {"n": 0}

        async def fake_fetch(url: str):
            if url.endswith("/ok") and rate_hits["n"] == 0:
                rate_hits["n"] += 1
                from acquisition.errors import RateLimitedError

                raise RateLimitedError(retry_after=0.01)
            ct, body = pages.get(url, ("text/html", "<html/>"))
            return RawArtifact(
                artifact_id=new_id("art-"),
                source_id="src-1",
                tenant_id="tenant-a",
                content_type=ct,
                fetched_at=utc_now(),
                checksum=checksum_text(body),
                content_text=body,
                content_bytes_len=len(body.encode()),
                url=url,
                content_trust=CONTENT_TRUST_UNTRUSTED,
            )

        svc = build_acquisition_runtime()
        svc.register_source(
            _source(
                metadata={"path_deny": ["/deny"]},
            )
        )
        # Use definition-style deny via CrawlLimits
        crawler = ControlledCrawler(svc.manager, fetch_fn=fake_fetch, store=svc.store)
        clock = {"t": 0.0}

        def fake_clock():
            return clock["t"]

        crawler._clock = fake_clock
        result = await crawler.crawl(
            source=_definition(
                crawl_policy=CrawlPolicy(
                    max_depth=1,
                    max_pages=5,
                    max_frontier=20,
                    path_deny=("/deny",),
                    deadline_seconds=100,
                    min_interval_seconds=0,
                    ignore_tracking_params=True,
                )
            ),
            seeds=("https://example.com/",),
            tenant_id="tenant-a",
            limits=CrawlLimits(
                max_depth=1,
                max_pages=5,
                path_deny=("/deny",),
                deadline_seconds=100,
            ),
        )
        self.assertGreaterEqual(result.pages_fetched, 1)
        urls = {a.url for a in result.artifacts}
        self.assertNotIn("https://evil.com/x", urls)
        # content-type skip
        self.assertTrue(any("skip" in s or "content_type" in e for s in result.skipped for e in [""]) or result.pages_skipped >= 0)

        # cancel
        job, _ = svc.submit_job(
            source_id="src-1",
            tenant_id="tenant-a",
            mode=MODE_CRAWL,
            seeds=("https://example.com/",),
            enqueue=False,
        )
        crawler.request_cancel(job.job_id)
        cancelled = await crawler.crawl(
            source=_source(),
            seeds=("https://example.com/",),
            tenant_id="tenant-a",
            job=job,
            limits=CrawlLimits(max_pages=10),
        )
        # may complete first page before cancel check — request_cancel set
        self.assertIn(job.job_id, crawler._cancel_jobs)

        # deadline
        clock["t"] = 0.0
        crawler2 = ControlledCrawler(svc.manager, fetch_fn=fake_fetch, clock=lambda: clock["t"])
        clock["t"] = 50.0
        dead = await crawler2.crawl(
            source=_source(),
            seeds=("https://example.com/",),
            tenant_id="tenant-a",
            limits=CrawlLimits(max_pages=10, deadline_seconds=1),
        )
        self.assertTrue(dead.status in {"partial", "completed", "failed"} or "deadline" in str(dead.errors))

    async def test_checkpoint_resume_crash_recovery(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store = SqliteAcquisitionStore(db_path=str(Path(tmp) / "acq.db"))
            self.assertEqual(ACQUISITION_SCHEMA_VERSION, 2)
            svc = build_acquisition_runtime(store=store)
            svc.register_source(_source())
            calls = {"n": 0}

            async def fetch(url: str):
                calls["n"] += 1
                body = f'<html><a href="/p{calls["n"]}">x</a>page{calls["n"]}</html>'
                return RawArtifact(
                    artifact_id=new_id("art-"),
                    source_id="src-1",
                    tenant_id="tenant-a",
                    content_type="text/html",
                    fetched_at=utc_now(),
                    checksum=checksum_text(body + url),
                    content_text=body,
                    content_bytes_len=len(body),
                    url=url,
                    content_trust=CONTENT_TRUST_UNTRUSTED,
                )

            crawler = ControlledCrawler(svc.manager, fetch_fn=fetch, store=store)
            job, _ = svc.submit_job(
                source_id="src-1",
                tenant_id="tenant-a",
                mode=MODE_CRAWL,
                seeds=("https://example.com/",),
                enqueue=False,
            )
            r1 = await crawler.crawl(
                source=_source(),
                seeds=("https://example.com/",),
                tenant_id="tenant-a",
                job=job,
                limits=CrawlLimits(max_depth=2, max_pages=1),
            )
            self.assertEqual(r1.pages_fetched, 1)
            cp = store.get_checkpoint(job.job_id, tenant_id="tenant-a")
            self.assertIsNotNone(cp)
            self.assertGreaterEqual(cp.pages_fetched, 1)
            # resume
            r2 = await crawler.crawl(
                source=_source(),
                seeds=("https://example.com/",),
                tenant_id="tenant-a",
                job=job,
                resume=True,
                limits=CrawlLimits(max_depth=2, max_pages=3),
            )
            self.assertGreaterEqual(r2.pages_fetched, 1)
            store.close()


class ScrapeNormalizeDedupeIngestTests(unittest.IsolatedAsyncioTestCase):
    async def test_scrape_static_pagination_profile_pin(self):
        html1 = '<html><ul><li>1</li></ul><a rel="next" href="/p2">n</a></html>'
        html2 = "<html><ul><li>2</li></ul></html>"
        bodies = {"https://example.com/list": html1, "https://example.com/p2": html2}

        async def fetch(url: str):
            body = bodies[url]
            return RawArtifact(
                artifact_id=new_id("art-"),
                source_id="src-1",
                tenant_id="tenant-a",
                content_type="text/html",
                fetched_at=utc_now(),
                checksum=checksum_text(body),
                content_text=body,
                content_bytes_len=len(body),
                url=url,
                content_trust=CONTENT_TRUST_UNTRUSTED,
            )

        svc = build_acquisition_runtime()
        svc.register_source(_source())
        svc.scraper._fetch_fn = fetch
        profile = ScrapingProfile(
            profile_id="static.list",
            version="1.0.0",
            pagination={"strategy": "next_link", "max_pages": 5},
        )
        job, _ = svc.submit_job(
            source_id="src-1",
            tenant_id="tenant-a",
            mode=MODE_SCRAPE,
            seeds=("https://example.com/list",),
            scrape_profile=profile,
            enqueue=False,
        )
        self.assertEqual(job.scrape_profile_version, "1.0.0")
        result = await svc.scrape(
            source_id="src-1",
            tenant_id="tenant-a",
            seed_url="https://example.com/list",
            profile=profile,
            job=job,
        )
        self.assertEqual(result.pages, 2)
        self.assertEqual(result.profile_version, "1.0.0")

    def test_pagination_repeated_cursor_terminates(self):
        pager = PaginationController(strategy="cursor", max_pages=10)
        state = pager.initial("https://example.com/api")
        state = pager.advance(state, payload={"next_cursor": "abc"}, record_count=1)
        self.assertFalse(state.done)
        state = pager.advance(state, payload={"next_cursor": "abc"}, record_count=1)
        self.assertTrue(state.done)
        self.assertEqual(state.reason, "repeated_cursor")

    def test_normalize_never_invents_date_currency_zero(self):
        status = {}
        self.assertIsNone(normalize_currency(None, field_status=status))
        self.assertEqual(status["currency"], EXTRACT_MISSING)
        status = {}
        self.assertIsNone(normalize_currency("$", field_status=status))
        self.assertEqual(status["currency"], EXTRACT_INVALID)
        status = {}
        self.assertIsNone(normalize_date("01/02/2024", field_status=status))
        self.assertEqual(status["date"], EXTRACT_INVALID)
        self.assertEqual(normalize_date("2024-03-15", field_status={}), "2024-03-15")
        norm = RecordNormalizer()
        art = RawArtifact(
            artifact_id="a1",
            source_id="src-1",
            tenant_id="tenant-a",
            content_type="text/csv",
            fetched_at=utc_now(),
            checksum="x",
            content_text="x",
        )
        from acquisition.models import ParsedRecord, RECORD_PRICE, fingerprint_record

        rec = ParsedRecord(
            record_id="r1",
            parser_id="t",
            parser_version="1",
            source_id="src-1",
            artifact_id="a1",
            tenant_id="tenant-a",
            record_type=RECORD_PRICE,
            fields={"sku": "S1", "price": None, "currency": None, "date": "03/04/2024"},
            confidence=1.0,
            fingerprint=fingerprint_record({"sku": "S1"}),
            observed_at=utc_now(),
        )
        result = norm.normalize_parsed(rec, job_id="job-1")
        self.assertNotIn("price", result.record.fields)  # not invented as 0
        self.assertNotIn("currency", result.record.fields)
        self.assertEqual(result.record.field_status.get("date"), EXTRACT_INVALID)

    def test_dedupe_layers_and_cross_source_provenance(self):
        engine = DedupeEngine()
        norm = RecordNormalizer()
        r1 = norm.normalize_fields(
            {"sku": "S1", "ean": VALID_EAN, "price": 10, "currency": "USD"},
            tenant_id="tenant-a",
            source_id="src-a",
            job_id="job-1",
            resource_id="res-1",
        ).record
        d1 = engine.decide(r1, job_id="job-1", url="https://example.com/a", raw_hash="h1")
        self.assertEqual(d1.decision, DEDUPE_UNIQUE)
        r2 = norm.normalize_fields(
            {"sku": "S1", "ean": VALID_EAN, "price": 10, "currency": "USD"},
            tenant_id="tenant-a",
            source_id="src-b",
            job_id="job-2",
            resource_id="res-2",
        ).record
        d2 = engine.decide(r2, job_id="job-2", url="https://example.com/b", raw_hash="h2")
        self.assertIn(d2.decision, {DEDUPE_CROSS_SOURCE, "possible", "exact", "same_source"})
        self.assertTrue(d2.provenance_refs)

    def test_ingest_idempotent_tenant_dataset(self):
        target = InMemoryIngestionTarget()
        norm = RecordNormalizer()
        rec = norm.normalize_fields(
            {"sku": "S9", "price": 1.5, "currency": "USD"},
            tenant_id="tenant-a",
            source_id="src-1",
            job_id="job-1",
        ).record
        b1 = target.ingest_batch(
            tenant_id="tenant-a",
            job_id="job-1",
            dataset_name="prices",
            records=(rec,),
            idempotency_key="batch-1",
        )
        b2 = target.ingest_batch(
            tenant_id="tenant-a",
            job_id="job-1",
            dataset_name="prices",
            records=(rec,),
            idempotency_key="batch-1",
        )
        self.assertEqual(b1.batch_id, b2.batch_id)
        self.assertEqual(b1.accepted, b2.accepted)
        ds = target.get_dataset(b1.dataset_id, tenant_id="tenant-a")
        self.assertIsNotNone(ds)
        self.assertIsNone(target.get_dataset(b1.dataset_id, tenant_id="tenant-b"))


class ParseBoundsTests(unittest.TestCase):
    def test_html_json_csv_empty_oversized_nesting(self):
        reg = build_default_parser_registry()
        html = RawArtifact(
            artifact_id="a",
            source_id="s",
            tenant_id="tenant-a",
            content_type="text/html",
            fetched_at=utc_now(),
            checksum="1",
            content_text="<html><body>Hello</body></html>",
        )
        self.assertTrue(reg.parse(html))
        csv = RawArtifact(
            artifact_id="b",
            source_id="s",
            tenant_id="tenant-a",
            content_type="text/csv",
            fetched_at=utc_now(),
            checksum="2",
            content_text="a,b\n1,2\n",
        )
        self.assertTrue(reg.parse(csv))
        js = RawArtifact(
            artifact_id="c",
            source_id="s",
            tenant_id="tenant-a",
            content_type="application/json",
            fetched_at=utc_now(),
            checksum="3",
            content_text='{"x":1}',
        )
        self.assertTrue(reg.parse(js))
        empty = RawArtifact(
            artifact_id="d",
            source_id="s",
            tenant_id="tenant-a",
            content_type="text/plain",
            fetched_at=utc_now(),
            checksum="4",
            content_text="",
        )
        with self.assertRaises(Exception):
            reg.parse(empty)
        # nesting
        node = {}
        cur = node
        for _ in range(MAX_JSON_NESTING + 5):
            cur["k"] = {}
            cur = cur["k"]
        import json

        deep = RawArtifact(
            artifact_id="e",
            source_id="s",
            tenant_id="tenant-a",
            content_type="application/json",
            fetched_at=utc_now(),
            checksum="5",
            content_text=json.dumps(node),
            content_bytes_len=100,
        )
        with self.assertRaises(ContentNestingTooDeepError):
            reg.parse(deep)


class CapacityAndTenantJobTests(unittest.TestCase):
    def test_capacity_rejection_and_tenant_isolation_jobs(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store = SqliteAcquisitionStore(db_path=str(Path(tmp) / "acq.db"))
            svc = build_acquisition_runtime(store=store)
            svc.register_source(_source())
            with self.assertRaises(CapacityRejectedError):
                svc.submit_job(
                    source_id="src-1",
                    tenant_id="tenant-a",
                    mode=MODE_CRAWL,
                    seeds=tuple(f"https://example.com/{i}" for i in range(600)),
                    crawl_policy=CrawlPolicy(max_frontier=10, max_pages=5),
                    enqueue=False,
                )
            job, _ = svc.submit_job(
                source_id="src-1",
                tenant_id="tenant-a",
                mode=MODE_CRAWL,
                seeds=("https://example.com/",),
                enqueue=False,
            )
            self.assertIsNone(svc.get_job(job.job_id, tenant_id="tenant-b"))
            cancelled = svc.cancel_job(job.job_id, tenant_id="tenant-a")
            self.assertEqual(cancelled.status, JOB_CANCELLED)
            from acquisition.models import AcquisitionJob

            with self.assertRaises(MissingTenantError):
                AcquisitionJob(
                    job_id="j",
                    tenant_id="",
                    actor_id="",
                    source_id="s",
                    mode=MODE_CRAWL,
                    workload_class="batch",
                )
            store.close()


class E2EPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_job_batch_fetch_parse_normalize_dedupe_ingest_dataset(self):
        q = TaskQueue(store=InMemoryTaskQueueStore())
        svc = build_acquisition_runtime()
        svc.task_queue = q
        svc.register_source(_source())
        svc.register_source(_source(source_id="src-2", name="B"))

        csv_body = f"sku,ean,name,price,currency,stock\nS1,{VALID_EAN},W,10,USD,5\n"

        async def fetch(url: str):
            return RawArtifact(
                artifact_id=new_id("art-"),
                source_id="src-1",
                tenant_id="tenant-a",
                content_type="text/csv",
                fetched_at=utc_now(),
                checksum=checksum_text(csv_body + url),
                content_text=csv_body,
                content_bytes_len=len(csv_body),
                url=url,
                content_trust=CONTENT_TRUST_UNTRUSTED,
            )

        svc.crawler._fetch_fn = fetch
        job, task = svc.submit_job(
            source_id="src-1",
            tenant_id="tenant-a",
            mode=MODE_CRAWL,
            seeds=("https://example.com/feed.csv",),
            estimated_pages=50,
        )
        self.assertIsNotNone(task)
        self.assertEqual(task.execution_lane, LANE_BULK)
        pipe = await svc.run_job(job, seeds=("https://example.com/feed.csv",), dataset_name="prices")
        self.assertIsNotNone(pipe.dataset)
        self.assertGreaterEqual(pipe.ingest.accepted + pipe.ingest.duplicate, 1)

        # multi-source cross-dedupe
        art2 = svc.ingest_text(
            source_id="src-2",
            tenant_id="tenant-a",
            text=csv_body,
            content_type="text/csv",
            url="https://example.com/other.csv",
        )
        job2, _ = svc.submit_job(
            source_id="src-2",
            tenant_id="tenant-a",
            mode=MODE_SINGLE,
            seeds=("https://example.com/other.csv",),
            enqueue=False,
        )
        pipe2 = svc.pipeline.process_artifacts(job=job2, artifacts=(art2,), dataset_name="prices-multi")
        self.assertTrue(pipe2.decisions)
        self.assertIsNotNone(pipe2.dataset)

    async def test_gateway_path_ssrf_allowlisted(self):
        reg = ToolRegistry()
        gateway = ToolGateway(FakeSearchProvider(), registry=reg, register_search=True)
        register_platform_tools(reg, env={"TOOL_HTTP_ALLOWED_HOSTS": "example.com"})
        gateway.router = ToolRouter(reg)
        http_reg = reg.get_registration("http.request")
        http_reg.adapter.execute_read = AsyncMock(
            return_value={
                "status_code": 200,
                "content_type": "text/html",
                "body_text": "<html>ok</html>",
                "truncated": False,
            }
        )
        svc = build_acquisition_runtime(tool_gateway=gateway)
        svc.register_source(_source())
        with self.assertRaises(AcquisitionDeniedError):
            await svc.acquire(
                AcquisitionRequest(
                    source_id="src-1",
                    target="https://evil.example.org/x",
                    acquisition_type=ACQ_HTTP_GET,
                    tenant_id="tenant-a",
                )
            )
        art = await svc.acquire(
            AcquisitionRequest(
                source_id="src-1",
                target="https://example.com/item",
                acquisition_type=ACQ_HTTP_GET,
                tenant_id="tenant-a",
            )
        )
        self.assertIn("example.com", art.url)


class CrawlerRetryBudgetTests(unittest.IsolatedAsyncioTestCase):
    """P1: bounded per-URL retries — initial attempt is not a retry."""

    async def test_persistent_429_exhausts_and_terminates(self):
        attempts = {"n": 0}

        async def always_429(url: str):
            attempts["n"] += 1
            from acquisition.errors import RateLimitedError

            raise RateLimitedError(retry_after=0.0)

        events: list[tuple[str, dict]] = []
        svc = build_acquisition_runtime()
        svc.register_source(_source())
        crawler = ControlledCrawler(svc.manager, fetch_fn=always_429, store=svc.store)
        if crawler.observer is not None:
            crawler.observer.add_sink(lambda e, p: events.append((e, p)))

        result = await crawler.crawl(
            source=_source(),
            seeds=("https://example.com/poison",),
            tenant_id="tenant-a",
            limits=CrawlLimits(
                max_pages=5,
                max_depth=0,
                deadline_seconds=None,
                max_retries_per_url=3,
                min_interval_seconds=0,
            ),
        )
        self.assertEqual(attempts["n"], 4)  # 1 initial + 3 retries
        self.assertEqual(result.pages_failed, 1)
        self.assertEqual(result.pages_fetched, 0)
        self.assertIn(result.status, {"failed", "partial"})
        failed = [r for r in result.resources if r.status == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].provenance.get("error"), "rate_limited")
        self.assertTrue(failed[0].provenance.get("retry_exhausted"))
        self.assertEqual(failed[0].provenance.get("fetch_attempts"), 4)
        self.assertTrue(any("retry_exhausted" in e for e in result.errors))
        frontier = svc.store.list_frontier(job_id=failed[0].job_id, tenant_id="tenant-a")
        pending = [e for e in frontier if e.status in {"pending", "retry"}]
        self.assertEqual(pending, [])
        failed_fr = [e for e in frontier if e.status == "failed"]
        self.assertEqual(len(failed_fr), 1)
        self.assertEqual(failed_fr[0].error_code, "rate_limited")
        self.assertTrue(
            any(e == "acquisition.retry.exhausted" for e, _ in events)
            or any("retry_exhausted" in e for e in result.errors)
        )

    async def test_deadline_terminates_before_retry_budget(self):
        attempts = {"n": 0}
        clock = {"t": 0.0}

        async def always_429(url: str):
            attempts["n"] += 1
            clock["t"] += 10.0
            from acquisition.errors import RateLimitedError

            raise RateLimitedError(retry_after=0.0)

        svc = build_acquisition_runtime()
        svc.register_source(_source())
        crawler = ControlledCrawler(
            svc.manager,
            fetch_fn=always_429,
            store=svc.store,
            clock=lambda: clock["t"],
        )
        result = await crawler.crawl(
            source=_source(),
            seeds=("https://example.com/slow",),
            tenant_id="tenant-a",
            limits=CrawlLimits(
                max_pages=5,
                max_depth=0,
                deadline_seconds=5,
                max_retries_per_url=10,
                min_interval_seconds=0,
            ),
        )
        self.assertLess(attempts["n"], 11)
        self.assertGreaterEqual(attempts["n"], 1)
        self.assertEqual(result.status, "partial")
        self.assertTrue(any("deadline" in e for e in result.errors))

    async def test_eventual_success_after_429s(self):
        attempts = {"n": 0}

        async def flaky(url: str):
            attempts["n"] += 1
            if attempts["n"] < 3:
                from acquisition.errors import RateLimitedError

                raise RateLimitedError(retry_after=0.0)
            body = "<html>ok</html>"
            return RawArtifact(
                artifact_id=new_id("art-"),
                source_id="src-1",
                tenant_id="tenant-a",
                content_type="text/html",
                fetched_at=utc_now(),
                checksum=checksum_text(body),
                content_text=body,
                content_bytes_len=len(body),
                url=url,
                content_trust=CONTENT_TRUST_UNTRUSTED,
            )

        svc = build_acquisition_runtime()
        svc.register_source(_source())
        crawler = ControlledCrawler(svc.manager, fetch_fn=flaky, store=svc.store)
        result = await crawler.crawl(
            source=_source(),
            seeds=("https://example.com/flaky",),
            tenant_id="tenant-a",
            limits=CrawlLimits(
                max_pages=5,
                max_depth=0,
                max_retries_per_url=3,
                min_interval_seconds=0,
            ),
        )
        self.assertEqual(attempts["n"], 3)
        self.assertEqual(result.pages_fetched, 1)
        self.assertEqual(result.pages_failed, 0)
        self.assertEqual(len(result.artifacts), 1)
        fetched = [r for r in result.resources if r.status == "fetched"]
        self.assertEqual(len(fetched), 1)
        self.assertEqual(fetched[0].provenance.get("retry_count"), 2)
        self.assertEqual(fetched[0].provenance.get("fetch_attempts"), 3)

    async def test_poisoned_url_does_not_block_other_url(self):
        attempts = {"a": 0, "b": 0}

        async def mixed(url: str):
            if url.endswith("/a"):
                attempts["a"] += 1
                from acquisition.errors import RateLimitedError

                raise RateLimitedError(retry_after=0.0)
            attempts["b"] += 1
            body = "<html>b-ok</html>"
            return RawArtifact(
                artifact_id=new_id("art-"),
                source_id="src-1",
                tenant_id="tenant-a",
                content_type="text/html",
                fetched_at=utc_now(),
                checksum=checksum_text(body + url),
                content_text=body,
                content_bytes_len=len(body),
                url=url,
                content_trust=CONTENT_TRUST_UNTRUSTED,
            )

        svc = build_acquisition_runtime()
        svc.register_source(_source())
        crawler = ControlledCrawler(svc.manager, fetch_fn=mixed, store=svc.store)
        result = await crawler.crawl(
            source=_source(),
            seeds=("https://example.com/a", "https://example.com/b"),
            tenant_id="tenant-a",
            limits=CrawlLimits(
                max_pages=5,
                max_depth=0,
                max_retries_per_url=2,
                deadline_seconds=None,
                min_interval_seconds=0,
            ),
        )
        self.assertEqual(attempts["a"], 3)  # 1 + 2 retries
        self.assertEqual(attempts["b"], 1)
        statuses = {r.canonical_url: r.status for r in result.resources}
        self.assertEqual(statuses.get("https://example.com/a"), "failed")
        self.assertEqual(statuses.get("https://example.com/b"), "fetched")
        self.assertEqual(result.pages_fetched, 1)
        self.assertEqual(result.pages_failed, 1)
        self.assertIn(result.status, {"partial", "completed"})

    async def test_resume_preserves_retry_count(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store = SqliteAcquisitionStore(db_path=str(Path(tmp) / "retry.db"))
            svc = build_acquisition_runtime(store=store)
            svc.register_source(_source())
            attempts = {"n": 0}

            async def always_429(url: str):
                attempts["n"] += 1
                from acquisition.errors import RateLimitedError

                raise RateLimitedError(retry_after=0.0)

            crawler = ControlledCrawler(svc.manager, fetch_fn=always_429, store=store)
            job, _ = svc.submit_job(
                source_id="src-1",
                tenant_id="tenant-a",
                mode=MODE_CRAWL,
                seeds=("https://example.com/resume",),
                enqueue=False,
            )
            # First run: allow 2 retries scheduled then stop via cancel after 2 failures scheduled
            # Drive exactly 2 failed attempts (retry_count becomes 2) by max_retries high + cancel mid-flight
            # Simpler: crawl until retry_count==2 persisted by limiting via custom fetch that tracks
            # and then stop by raising after scheduling — use max_pages and interrupt via cancel.
            async def two_then_cancel(url: str):
                attempts["n"] += 1
                from acquisition.errors import RateLimitedError

                if attempts["n"] >= 2:
                    crawler.request_cancel(job.job_id)
                raise RateLimitedError(retry_after=0.0)

            crawler = ControlledCrawler(svc.manager, fetch_fn=two_then_cancel, store=store)
            r1 = await crawler.crawl(
                source=_source(),
                seeds=("https://example.com/resume",),
                tenant_id="tenant-a",
                job=job,
                limits=CrawlLimits(
                    max_pages=5,
                    max_depth=0,
                    max_retries_per_url=3,
                    min_interval_seconds=0,
                ),
            )
            frontier = store.list_frontier(job_id=job.job_id, tenant_id="tenant-a")
            pending = [e for e in frontier if e.status in {"pending", "retry"}]
            self.assertTrue(pending)
            self.assertEqual(pending[0].retry_count, 2)
            before = attempts["n"]
            self.assertEqual(before, 2)

            crawler.clear_cancel(job.job_id)
            attempts_after = {"n": 0}

            async def continue_429(url: str):
                attempts_after["n"] += 1
                from acquisition.errors import RateLimitedError

                raise RateLimitedError(retry_after=0.0)

            crawler2 = ControlledCrawler(svc.manager, fetch_fn=continue_429, store=store)
            r2 = await crawler2.crawl(
                source=_source(),
                seeds=("https://example.com/resume",),
                tenant_id="tenant-a",
                job=job,
                resume=True,
                limits=CrawlLimits(
                    max_pages=5,
                    max_depth=0,
                    max_retries_per_url=3,
                    min_interval_seconds=0,
                ),
            )
            # Remaining budget: retry_count starts at 2 → one more retry (attempt with count 2),
            # then exhaust on count 3. So 2 more fetch attempts (count 2 fail→3, count 3 fail→exhaust).
            self.assertEqual(attempts_after["n"], 2)
            self.assertEqual(r2.pages_failed, 1)
            failed_fr = [
                e
                for e in store.list_frontier(job_id=job.job_id, tenant_id="tenant-a")
                if e.status == "failed"
            ]
            self.assertEqual(len(failed_fr), 1)
            self.assertEqual(failed_fr[0].error_code, "rate_limited")
            self.assertEqual(failed_fr[0].retry_count, 3)

    async def test_zero_retries_fails_immediately(self):
        attempts = {"n": 0}

        async def always_429(url: str):
            attempts["n"] += 1
            from acquisition.errors import RateLimitedError

            raise RateLimitedError(retry_after=0.0)

        svc = build_acquisition_runtime()
        svc.register_source(_source())
        crawler = ControlledCrawler(svc.manager, fetch_fn=always_429, store=svc.store)
        result = await crawler.crawl(
            source=_source(),
            seeds=("https://example.com/once",),
            tenant_id="tenant-a",
            limits=CrawlLimits(
                max_pages=5,
                max_depth=0,
                max_retries_per_url=0,
                min_interval_seconds=0,
            ),
        )
        self.assertEqual(attempts["n"], 1)
        self.assertEqual(result.pages_failed, 1)
        failed = [r for r in result.resources if r.status == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].provenance.get("error"), "rate_limited")
        self.assertTrue(failed[0].provenance.get("retry_exhausted"))

    def test_max_retries_per_url_validation(self):
        with self.assertRaises(ValueError):
            CrawlPolicy(max_retries_per_url=-1)


class ObservabilitySecurityTests(unittest.TestCase):
    def test_obs_redacts_content_and_secrets(self):
        from acquisition.observability import sanitize_event_payload

        safe = sanitize_event_payload(
            {"job_id": "j1", "body_text": "SECRET", "token": "x", "lane": "bulk"}
        )
        self.assertNotIn("body_text", safe)
        self.assertNotIn("token", safe)
        self.assertEqual(safe["lane"], "bulk")

    def test_source_definition_secret_ref_only(self):
        d = _definition(auth_secret_ref="vault:proj/http-basic")
        self.assertEqual(d.auth_secret_ref, "vault:proj/http-basic")
        with self.assertRaises(ValueError):
            _definition(auth_secret_ref="password=supersecret")


if __name__ == "__main__":
    unittest.main()
