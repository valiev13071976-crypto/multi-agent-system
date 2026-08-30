"""Block 12 — SEO & Digital Marketing."""

from seo_marketing.runtime import SeoMarketingRuntime, build_seo_marketing_runtime, seo_marketing_enabled
from seo_marketing.service import SeoMarketingService

__all__ = [
    "SeoMarketingService",
    "SeoMarketingRuntime",
    "build_seo_marketing_runtime",
    "seo_marketing_enabled",
]
