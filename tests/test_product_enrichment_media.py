"""Product enrichment pipeline: product image acquisition, validation,
dedup and Aspro-destination processing (requirements 5-10). Uses the
deterministic ``FakeImageFetcher`` -- zero real network calls."""

from __future__ import annotations

import io
import unittest

from PIL import Image

from product_enrichment.identity import resolve_identity
from product_enrichment.media import MediaAcquisitionService
from product_enrichment.media_fetch import FakeImageFetcher
from product_enrichment.models import (
    MediaCandidateInput,
    MediaResult,
    ProductIdentityQuery,
    SOURCE_AUTHORIZED_DISTRIBUTOR,
    SOURCE_MANUFACTURER,
)


def _png(w: int = 400, h: int = 400, color=(10, 20, 30)) -> bytes:
    img = Image.new("RGB", (w, h), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def _identity():
    return resolve_identity(ProductIdentityQuery(brand="LG", model="55MRGB86B6A.ARUG", ean="8806096824788"))


class MediaAcquisitionServiceTests(unittest.TestCase):
    def test_valid_candidate_produces_preview_and_detail_assets(self):
        url = "https://lg.com/photo.png"
        fetcher = FakeImageFetcher({url: _png()})
        service = MediaAcquisitionService(fetcher=fetcher)
        result = _run(
            service.acquire([MediaCandidateInput(url=url, source_type=SOURCE_MANUFACTURER)], identity=_identity())
        )
        roles = {asset.role for asset in result.assets}
        self.assertIn("preview", roles)
        self.assertIn("detail", roles)
        self.assertEqual(result.status, MediaResult.STATUS_READY)
        for asset in result.assets:
            self.assertTrue(asset.base64_content)
            self.assertFalse(asset.generated)
            # The external URL is retained ONLY as provenance -- never the
            # thing a Bitrix write would receive (that is base64_content).
            self.assertEqual(asset.source_url, url)

    def test_broken_non_image_bytes_rejected(self):
        url = "https://lg.com/not-an-image.html"
        fetcher = FakeImageFetcher({url: b"<html>not an image</html>"})
        service = MediaAcquisitionService(fetcher=fetcher)
        result = _run(service.acquire([MediaCandidateInput(url=url)], identity=_identity()))
        self.assertEqual(result.assets, ())
        self.assertEqual(result.status, MediaResult.STATUS_UNRESOLVED)
        self.assertEqual(len(result.rejected_candidates), 1)
        self.assertTrue(result.rejected_candidates[0].reason.startswith("invalid_image"))

    def test_download_failure_does_not_abort_other_candidates(self):
        good_url = "https://lg.com/good.png"
        bad_url = "https://lg.com/broken.png"
        fetcher = FakeImageFetcher({good_url: _png()}, error_urls=(bad_url,))
        service = MediaAcquisitionService(fetcher=fetcher)
        result = _run(
            service.acquire(
                [MediaCandidateInput(url=bad_url), MediaCandidateInput(url=good_url)], identity=_identity()
            )
        )
        self.assertEqual(result.status, MediaResult.STATUS_READY)
        self.assertTrue(any(r.source_url == bad_url for r in result.rejected_candidates))

    def test_too_small_image_rejected_for_low_resolution(self):
        url = "https://lg.com/tiny.png"
        fetcher = FakeImageFetcher({url: _png(w=20, h=20)})
        service = MediaAcquisitionService(fetcher=fetcher)
        result = _run(service.acquire([MediaCandidateInput(url=url)], identity=_identity()))
        self.assertEqual(result.assets, ())
        self.assertEqual(result.rejected_candidates[0].reason, "resolution_too_low")

    def test_extreme_aspect_ratio_rejected(self):
        url = "https://lg.com/banner.png"
        fetcher = FakeImageFetcher({url: _png(w=2000, h=100)})
        service = MediaAcquisitionService(fetcher=fetcher)
        result = _run(service.acquire([MediaCandidateInput(url=url)], identity=_identity()))
        self.assertEqual(result.assets, ())
        self.assertEqual(result.rejected_candidates[0].reason, "aspect_ratio_out_of_range")

    def test_duplicate_content_hash_is_deduplicated(self):
        same_bytes = _png()
        url_a = "https://lg.com/a.png"
        url_b = "https://distributor.example/mirror-of-a.png"
        fetcher = FakeImageFetcher({url_a: same_bytes, url_b: same_bytes})
        service = MediaAcquisitionService(fetcher=fetcher)
        result = _run(
            service.acquire(
                [MediaCandidateInput(url=url_a), MediaCandidateInput(url=url_b)], identity=_identity()
            )
        )
        self.assertTrue(any(r.reason == "duplicate_content_hash" for r in result.rejected_candidates))

    def test_extra_candidates_become_gallery_role(self):
        fetcher = FakeImageFetcher(
            {
                "https://lg.com/main.png": _png(color=(1, 1, 1)),
                "https://lg.com/side1.png": _png(color=(2, 2, 2)),
                "https://lg.com/side2.png": _png(color=(3, 3, 3)),
            }
        )
        service = MediaAcquisitionService(fetcher=fetcher)
        result = _run(
            service.acquire(
                [
                    MediaCandidateInput(url="https://lg.com/main.png", source_type=SOURCE_MANUFACTURER),
                    MediaCandidateInput(url="https://lg.com/side1.png", source_type=SOURCE_AUTHORIZED_DISTRIBUTOR),
                    MediaCandidateInput(url="https://lg.com/side2.png", source_type=SOURCE_AUTHORIZED_DISTRIBUTOR),
                ],
                identity=_identity(),
            )
        )
        gallery = [a for a in result.assets if a.role == "gallery"]
        self.assertEqual(len(gallery), 2)

    def test_no_candidates_supplied_yields_unresolved_status(self):
        service = MediaAcquisitionService(fetcher=FakeImageFetcher({}))
        result = _run(service.acquire([], identity=_identity()))
        self.assertEqual(result.status, MediaResult.STATUS_UNRESOLVED)
        self.assertEqual(result.assets, ())

    def test_resized_derivative_never_upscales_a_small_source(self):
        # A source already smaller than the "detail" destination bound
        # must never be stretched/upscaled -- Image.thumbnail (used by
        # resize_image's "contain" fit) only ever shrinks.
        url = "https://lg.com/small-but-valid.png"
        fetcher = FakeImageFetcher({url: _png(w=150, h=150)})
        service = MediaAcquisitionService(fetcher=fetcher)
        result = _run(service.acquire([MediaCandidateInput(url=url)], identity=_identity()))
        detail = next(a for a in result.assets if a.role == "detail")
        self.assertLessEqual(detail.width, 150)
        self.assertLessEqual(detail.height, 150)

    def test_filenames_are_namespaced_by_identity_key_never_a_bare_original_filename(self):
        url = "https://lg.com/photo.png"
        fetcher = FakeImageFetcher({url: _png()})
        service = MediaAcquisitionService(fetcher=fetcher)
        identity = _identity()
        result = _run(service.acquire([MediaCandidateInput(url=url)], identity=identity))
        for asset in result.assets:
            self.assertTrue(asset.filename.startswith(identity.identity_key))


if __name__ == "__main__":
    unittest.main()
