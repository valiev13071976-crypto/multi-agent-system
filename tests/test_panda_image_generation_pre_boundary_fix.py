"""PANDA IMAGE GENERATION — FINAL PRE-BOUNDARY FIX.

Traced the exact production entry point (POST /api/v1/business-assistant/
requests -> business_assistant_api.service.submit_async -> BusinessAssistant
Service.respond_conversationally_async -> WorkflowPandaConversationGateway.
respond -> business_assistant.action_continuation.resolve_action_turn ->
ToolGateway.invoke -> ProductMediaToolAdapter.execute_read -> _chat_generate)
for the exact reported prompt "Нарисуй красную машину на белом фоне." --
Russian intent detection, family/action resolution, capability/permission
checks, ToolGateway routing and ProductMediaToolAdapter dispatch all resolve
correctly and DO reach the image-generation execution boundary (proven below,
reproduced through the real entry point with a mock provider).

So why did the deploy logs show ZERO `image_generation_started` /
`image_generation_failed` / `product_media_construction_failed` records --
not just after this one smoke test, but on every prior round too, success or
failure? Because nothing in this app's startup path ever called
`logging.basicConfig()`. Python's root logger defaults to level WARNING with
no handler attached. Every one of those diagnostics is logged at INFO level
(`product_media/tools.py::_log_image_event` -> `_log.info(...)`), so they were
UNCONDITIONALLY dropped before ever reaching stdout/Railway logs --
regardless of whether the code path was ever reached, called the real
provider, succeeded, or failed. The "0 records" evidence this task's premise
rests on is not proof the request never reached image_generation_started; it
is proof the app never had a logging configuration capable of showing that
either way.

That observability gap sits directly in the region this task requires
checking (it swallows any error signal a pre/post-boundary failure would
have produced), so it is fixed here per the task's own allowance. It is not
the whole fix: `product_media/tools.py::execute_read()` called
`_chat_generate()`/`_chat_edit()` -- which make a genuinely BLOCKING,
synchronous HTTP request to the real image provider via a synchronous
`httpx.Client` -- directly inline on the async method, with no `await`
anywhere inside. Proven directly (see `EventLoopNotBlockedDuringGenerationTests`
below): a blocking call made this way freezes the ENTIRE single asyncio
event loop for its whole duration, and cannot ever be interrupted by
`asyncio.wait_for(execute_read(...), timeout=60)` in ToolGateway -- that
timeout can only fire at an await point, and a plain blocking call yields
none until it already returns on its own. On this app's actual deployment
(Procfile / railway.toml: `uvicorn main:app ...` with no `--workers` flag,
i.e. one process, one event loop), a slow-but-real provider call freezes
every other concurrent request on that worker -- including Railway's own
/health probe -- for the call's entire duration, and makes the tool's
declared 60s timeout a no-op rather than a real safety net.

Fix:
1. `main.py` now configures the root logger (INFO, stdout) at import time,
   before any other module can emit a log record -- restoring visibility
   into image_generation_started/succeeded/failed and
   product_media_construction_failed for future diagnosis.
2. `product_media/tools.py::execute_read()` now runs `_chat_generate`/
   `_chat_edit` via `asyncio.to_thread(...)` instead of calling them inline,
   so the blocking provider call runs off the event loop -- concurrent
   requests keep making progress, and ToolGateway's existing 60s timeout can
   actually fire on a genuinely hung/slow call instead of being bypassed.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import tempfile
import time
import unittest

import numpy as np
from PIL import Image

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _real_large_png(min_bytes: int, edge: int = 900) -> bytes:
    rng = np.random.default_rng(23)
    arr = rng.integers(0, 256, (edge, edge, 3), dtype="uint8")
    raw = io.BytesIO()
    Image.fromarray(arr).save(raw, format="PNG")
    data = raw.getvalue()
    assert len(data) >= min_bytes, f"fixture too small: {len(data)} < {min_bytes}"
    return data


class RedCarExactPromptFullBusinessAssistantEntryPointTests(unittest.TestCase):
    """КЛЮЧЕВОЙ ТЕСТ: the exact reported production prompt, through the SAME
    Business Assistant HTTP entry point and routing path production uses --
    not ProductMediaService or image.generate called directly."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        os.environ["BA_API_DB_PATH"] = os.path.join(cls.tmp, "ba.sqlite")
        os.environ["BA_API_UPLOAD_DIR"] = os.path.join(cls.tmp, "uploads")
        os.makedirs(os.environ["BA_API_UPLOAD_DIR"], exist_ok=True)
        os.environ["SECURITY_AUTH_MODE"] = "required"
        os.environ["PANDA_API_KEYS"] = "key-car|tenant-car|user-car|user|secret-car"
        os.environ["PRODUCT_MEDIA_DB_PATH"] = ":memory:"
        os.environ.pop("MEDIA_IMAGE_PROVIDER", None)

        import importlib

        import main as main_mod

        cls.main = importlib.reload(main_mod)

        from fastapi.testclient import TestClient

        cls.client = TestClient(cls.main.app)

    @classmethod
    def tearDownClass(cls):
        import shutil

        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_red_car_prompt_reaches_image_generation_boundary_and_succeeds(self):
        from unittest.mock import Mock

        from integrations.production.adapters.media import OpenAIImageGenerationProvider

        provider = OpenAIImageGenerationProvider(api_key="sk-test-not-a-real-openai-key", model="gpt-image-1")
        raw = _real_large_png(1_300_000)
        body = json.dumps({"data": [{"b64_json": base64.b64encode(raw).decode()}]}).encode()
        http_response = Mock()
        http_response.status_code = 200
        http_response.content = body
        http_response.headers = {}
        http_response.json = Mock(return_value=json.loads(body))
        inner_client = Mock()
        inner_client.request = Mock(return_value=http_response)
        provider._http._client = inner_client

        self.main.side_effect_runtime.product_media_service.generator = provider

        with self.assertLogs("product_media.image_generation", level="INFO") as captured:
            submit = self.client.post(
                "/api/v1/business-assistant/requests",
                headers={"X-API-Key": "secret-car"},
                json={"message": "Нарисуй красную машину на белом фоне."},
            )
        self.assertEqual(submit.status_code, 200, submit.text)
        request_id = submit.json()["request_id"]

        # PROOF the request reached the instrumented image-generation execution
        # boundary via the real entry/routing path (not a mock provider call
        # site) -- exactly the log line previously invisible in Railway logs
        # regardless of whether this ever fired.
        joined = "\n".join(captured.output)
        self.assertIn("event=image_generation_started", joined)
        self.assertIn("tool_id=image.generate", joined)
        self.assertIn("event=image_generation_succeeded", joined)

        result = self.client.get(
            f"/api/v1/business-assistant/requests/{request_id}/result",
            headers={"X-API-Key": "secret-car"},
        ).json()

        final_answer = str(result.get("final_answer") or "")
        self.assertNotIn("Не получилось", final_answer, result)
        self.assertIn("Готово", final_answer, result)
        artifacts = result.get("artifacts") or []
        self.assertEqual(len(artifacts), 1, result)
        self.assertEqual(artifacts[0]["mime_type"], "image/png")
        view_url = artifacts[0]["view_url"]
        self.assertTrue(view_url)

        media = self.client.get(view_url, headers={"X-API-Key": "secret-car"})
        self.assertEqual(media.status_code, 200)
        self.assertTrue(media.headers["content-type"].startswith("image/"))
        self.assertEqual(media.content, raw)


class EventLoopNotBlockedDuringGenerationTests(unittest.IsolatedAsyncioTestCase):
    """FUNCTIONAL fix proof: a slow (but successful) synchronous provider
    call must not freeze the whole asyncio event loop -- another concurrent
    coroutine (e.g. a second request, or Railway's /health probe on this
    single-worker deployment) must keep making progress while image
    generation is in flight."""

    async def test_concurrent_coroutine_keeps_running_during_slow_generation(self):
        from product_media.tools import ProductMediaToolAdapter
        from tools.models import ToolRequest

        class SlowSyncProvider:
            provider_id = "slow-sync"
            model = "test-model"

            def generate(self, **kwargs):
                time.sleep(0.4)  # genuinely blocking, no await -- like a real HTTP call
                from product_media.providers.fake import ProviderResult

                return ProviderResult(data=_real_large_png(2000, edge=32), mime_type="image/png", provider_id="slow-sync", profile_version="1.0.0")

        from product_media.runtime import build_product_media_runtime

        service = build_product_media_runtime(db_path=":memory:")
        service.generator = SlowSyncProvider()
        adapter = ProductMediaToolAdapter(service)

        request = ToolRequest(
            request_id="r1",
            workflow_id="w1",
            task_id="t1",
            tenant_id="tenant-a",
            user_id="user-a",
            tool_id="image.generate",
            operation="generate",
            arguments={"scene_description": "красная машина на белом фоне"},
        )

        ticks = 0

        async def ticker():
            nonlocal ticks
            for _ in range(20):
                await asyncio.sleep(0.02)
                ticks += 1

        t0 = time.monotonic()
        _, tick_result = await asyncio.gather(
            adapter.execute_read(request, {}),
            ticker(),
        )
        elapsed = time.monotonic() - t0

        # The ticker only accumulates ticks if the event loop is free to run it
        # WHILE the (0.4s) blocking provider call is in flight -- if execute_read
        # ran that blocking call inline (no asyncio.to_thread), the loop would
        # be frozen for the whole 0.4s and the ticker would starve.
        self.assertGreater(ticks, 10, "event loop must not be blocked during generation")
        self.assertLess(elapsed, 0.4 + 0.25, "concurrent work must overlap, not serialize, with generation")


if __name__ == "__main__":
    unittest.main()
