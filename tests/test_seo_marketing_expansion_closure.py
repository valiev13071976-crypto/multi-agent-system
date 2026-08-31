"""SEO & Digital Marketing Platform — applied expansion closure tests."""

from __future__ import annotations

import tempfile
import unittest

from seo_marketing.errors import SEO_CANCELLED, SEO_FACT_UNSUPPORTED, SEO_CONFLICT, SeoMarketingError
from seo_marketing.keywords import cluster_keywords, normalize_keywords
from seo_marketing.performance import audit_performance, build_performance_observation, get_cwv_budget
from seo_marketing.platform_models import PLATFORM_SCHEMA_VERSION, SEMANTIC_CORE_VERSION, CWV_BUDGET_VERSION
from seo_marketing.rank import classify_rank_change, compare_rank_history, ingest_rank_observation
from seo_marketing.service import SeoMarketingService
from seo_marketing.sqlite_store import SqliteSeoStore
from seo_marketing.technical import (
    analyze_robots_txt,
    analyze_sitemap_entries,
    analyze_structured_data,
    analyze_technical_snapshot,
    recommend_internal_links,
)
from seo_marketing.content_brief import build_seo_content_brief, brief_to_content_factory_context
from seo_marketing.capabilities import CAP_SEO_KEYWORD_ANALYZE, CAP_SEO_TECHNICAL_READ, CAP_SEO_PERFORMANCE_READ


def _svc() -> SeoMarketingService:
    return SeoMarketingService(SqliteSeoStore(":memory:"))


class ContractTests(unittest.TestCase):
    def test_schema_versions(self):
        self.assertEqual(PLATFORM_SCHEMA_VERSION, "1.0.0")
        self.assertTrue(SEMANTIC_CORE_VERSION)
        self.assertTrue(CWV_BUDGET_VERSION)


class SemanticCoreTests(unittest.TestCase):
    def test_versioned_core_and_stable_clusters(self):
        svc = _svc()
        site = svc.register_site(tenant_id="tenant-a", domain="https://example.com")
        seeds = [{"text": "buy shoes"}, {"text": "buy boots"}, {"text": "how to clean shoes"}]
        a = svc.build_semantic_core(
            tenant_id="tenant-a",
            site_id=site.site_id,
            seeds=seeds,
            version=1,
            capabilities=(CAP_SEO_KEYWORD_ANALYZE,),
        )
        b = svc.build_semantic_core(
            tenant_id="tenant-a",
            site_id=site.site_id,
            seeds=seeds,
            version=2,
            capabilities=(CAP_SEO_KEYWORD_ANALYZE,),
        )
        self.assertEqual(a["version"], 1)
        self.assertEqual(b["version"], 2)
        self.assertEqual(sorted(a["cluster_ids"]), sorted(b["cluster_ids"]))


class ClusterStabilityTests(unittest.TestCase):
    def test_cluster_ids_stable_across_runs(self):
        kws = normalize_keywords(
            [{"text": "red shoes"}, {"text": "red boots"}],
            tenant_id="tenant-a",
            site_id="site-1",
            source="seed",
        )
        c1 = cluster_keywords(kws, tenant_id="tenant-a", site_id="site-1")
        c2 = cluster_keywords(kws, tenant_id="tenant-a", site_id="site-1")
        self.assertEqual({c.cluster_id for c in c1}, {c.cluster_id for c in c2})


class TechnicalExpansionTests(unittest.TestCase):
    def test_h1_thin_sitemap_structured_orphan(self):
        audit = analyze_technical_snapshot(
            tenant_id="tenant-a",
            site_id="s1",
            snapshot_id="snap1",
            pages=[
                {
                    "url": "https://example.com/",
                    "title": "Home",
                    "h1": "Home",
                    "status_code": 200,
                    "is_home": True,
                    "page_type": "HOME",
                    "word_count": 40,
                    "structured_data": [{"type": "WebSite"}],
                    "in_sitemap": True,
                },
                {
                    "url": "https://example.com/article",
                    "title": "Article",
                    "h1": "",
                    "status_code": 200,
                    "page_type": "ARTICLE",
                    "word_count": 40,
                    "expect_in_sitemap": True,
                    "in_sitemap_urls": ["https://example.com/"],
                },
            ],
            links=[{"source": "https://example.com/", "target": "https://example.com/"}],
        )
        codes = {i.code for i in audit.issues}
        self.assertIn("missing_h1", codes)
        self.assertIn("thin_content_candidate", codes)
        self.assertIn("orphan_candidate", codes)
        self.assertIn("sitemap_missing_url", codes)

    def test_robots_and_sitemap_helpers(self):
        self.assertTrue(analyze_robots_txt(""))
        self.assertTrue(any(f["code"] == "robots_broad_disallow" for f in analyze_robots_txt("User-agent: *\nDisallow: /\n")))
        issues = analyze_sitemap_entries(
            [{"url": "https://example.com/x", "status_code": 404}, {"url": "https://example.com/x", "status_code": 200}]
        )
        self.assertTrue(any(i.code == "sitemap_non_200" for i in issues))

    def test_structured_data_and_link_recs(self):
        findings = analyze_structured_data([{"url": "https://example.com/p", "structured_data": [{"type": "Product"}]}])
        self.assertTrue(findings[0].present)
        recs = recommend_internal_links(
            tenant_id="tenant-a",
            site_id="s1",
            pages=[
                {"url": "https://example.com/", "is_home": True, "title": "Home"},
                {"url": "https://example.com/orphan", "title": "Orphan"},
            ],
            links=[],
        )
        self.assertTrue(recs)
        self.assertEqual(recs[0].status, "RECOMMENDATION_ONLY")


class CWVTests(unittest.TestCase):
    def test_versioned_budget_and_field_lab_separation(self):
        lab = get_cwv_budget("LAB")
        field = get_cwv_budget("FIELD")
        self.assertEqual(lab.measurement_type, "LAB")
        self.assertEqual(field.measurement_type, "FIELD")
        obs_lab = build_performance_observation(
            tenant_id="tenant-a", page_id="p1", metric="LCP", value=3.0, unit="s", measurement_type="LAB", source="fake"
        )
        obs_field = build_performance_observation(
            tenant_id="tenant-a", page_id="p1", metric="LCP", value=3.0, unit="s", measurement_type="FIELD", source="fake"
        )
        with self.assertRaises(SeoMarketingError) as ctx:
            audit_performance(tenant_id="tenant-a", site_id="s1", observations=[obs_lab, obs_field])
        self.assertEqual(ctx.exception.code, SEO_CONFLICT)


class ContentBriefTests(unittest.TestCase):
    def test_fact_lock_and_cf_context(self):
        brief = build_seo_content_brief(
            tenant_id="tenant-a",
            site_id="s1",
            primary_keyword="running shoes",
            title_recommendation="Running shoes guide",
            h1_recommendation="Running shoes",
            meta_recommendation="Learn about running shoes",
        )
        ctx = brief_to_content_factory_context(brief)
        self.assertEqual(ctx["delegate_generation_to"], "content_intel")
        with self.assertRaises(SeoMarketingError) as ctx_err:
            build_seo_content_brief(
                tenant_id="tenant-a",
                site_id="s1",
                primary_keyword="x",
                title_recommendation="guaranteed #1 free delivery tomorrow",
            )
        self.assertEqual(ctx_err.exception.code, SEO_FACT_UNSUPPORTED)

    def test_service_content_brief(self):
        svc = _svc()
        site = svc.register_site(tenant_id="tenant-a", domain="https://example.com")
        out = svc.create_content_brief(
            tenant_id="tenant-a",
            site_id=site.site_id,
            primary_keyword="widgets",
            product_facts={"sku": "W1"},
        )
        self.assertTrue(out["brief_id"])
        self.assertEqual(out["content_factory_context"]["delegate_generation_to"], "content_intel")


class RankTests(unittest.TestCase):
    def test_rank_history_not_overwritten(self):
        svc = _svc()
        site = svc.register_site(tenant_id="tenant-a", domain="https://example.com")
        svc.record_rank(
            tenant_id="tenant-a",
            site_id=site.site_id,
            keyword="shoes",
            page_url="https://example.com/shoes",
            position=8.0,
            observed_at="2026-01-01T00:00:00+00:00",
        )
        svc.record_rank(
            tenant_id="tenant-a",
            site_id=site.site_id,
            keyword="shoes",
            page_url="https://example.com/shoes",
            position=5.0,
            observed_at="2026-02-01T00:00:00+00:00",
        )
        deltas = svc.rank_history_deltas(tenant_id="tenant-a")
        self.assertEqual(len(deltas), 1)
        self.assertEqual(deltas[0]["change"], "improved")
        self.assertEqual(classify_rank_change(None, 3.0), "new")


class FeedbackNoLoopTests(unittest.TestCase):
    def test_feedback_terminates_without_global_mutation(self):
        svc = _svc()
        site = svc.register_site(tenant_id="tenant-a", domain="https://example.com")
        result = svc.feedback_cycle(
            tenant_id="tenant-a",
            site_id=site.site_id,
            baseline_metrics={"clicks": 10},
            post_metrics={"clicks": 12},
            what_changed="meta_title",
        )
        self.assertTrue(result["terminated"])
        self.assertFalse(result["global_mutation"])
        self.assertEqual(len(result["next_recommendations"]), 1)
        self.assertIn("correlation_not_causation", result["limitations"])


class JobCancelTests(unittest.TestCase):
    def test_cancel_job(self):
        svc = _svc()
        site = svc.register_site(tenant_id="tenant-a", domain="https://example.com")
        job = svc.start_keyword_job(
            tenant_id="tenant-a",
            site_id=site.site_id,
            seeds=[{"text": f"kw{i}"} for i in range(5)],
            bulk=True,
        )
        cancelled = svc.cancel_job(tenant_id="tenant-a", job_id=job["job_id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["code"], SEO_CANCELLED)


class ActionPlanTests(unittest.TestCase):
    def test_bounded_action_plan(self):
        svc = _svc()
        site = svc.register_site(tenant_id="tenant-a", domain="https://example.com")
        recs = [{"type": "TECHNICAL_FIX", "i": i} for i in range(100)]
        plan = svc.build_action_plan(tenant_id="tenant-a", site_id=site.site_id, recommendations=recs)
        self.assertLessEqual(plan["recommendation_count"], 50)
        self.assertEqual(plan["cms_write"], "REQUIRES_SIDE_EFFECT_GOVERNANCE")


class TenantTests(unittest.TestCase):
    def test_cross_tenant_page_denied(self):
        svc = _svc()
        site = svc.register_site(tenant_id="tenant-a", domain="https://example.com")
        page = svc.register_page(tenant_id="tenant-a", site_id=site.site_id, url="https://example.com/p")
        self.assertIsNone(svc.get_page(page.page_id, tenant_id="tenant-b"))


if __name__ == "__main__":
    unittest.main()
