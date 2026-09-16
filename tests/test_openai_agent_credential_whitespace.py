"""PANDA -- defensive whitespace trimming for ``OpenAIAgent`` credentials.

PRODUCTION-SHAPED FAILURE MODE found while closing the canonical-Workset
single-authority-orchestration task: a secret-injection pipeline (e.g.
Cloud Agent secrets) can append a trailing newline to ``OPENAI_API_KEY``
without that being visible anywhere. ``agents.openai_agent.OpenAIAgent``
is the ONE shared one-shot model-call seam both
``data_intel.nl_plan_llm`` (the canonical table-execution/product-
selection/field-query/write-plan-query semantic boundary) and
``managed_agent_poc`` rely on -- an untrimmed key there makes httpx
reject the ``Authorization`` header outright (``httpx.LocalProtocolError:
Illegal header value ...\\n``) BEFORE any network call, turning a
perfectly valid credential into a hard technical failure
(``ModelPlanError(status=MODEL_ERROR, reason_code=model_call_failed)``)
for EVERY turn -- including a plain "just analyze this spreadsheet"
turn that should have deferred to the managed agent instead. Several
other credential readers in this repo already ``.strip()`` for exactly
this reason (``integrations/production/adapters/speech.py``,
``production_validation/providers_live.py``, ``product_media/
readiness.py``); ``OpenAIAgent.__init__`` now does the same.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock


class OpenAIAgentCredentialWhitespaceTests(unittest.TestCase):
    def test_trailing_newline_in_api_key_and_model_is_stripped(self):
        from agents.openai_agent import OpenAIAgent

        with mock.patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "sk-real-key\n", "OPENAI_MODEL": "gpt-4o-mini\n"},
        ):
            agent = OpenAIAgent()
        self.assertEqual(agent.api_key, "sk-real-key")
        self.assertEqual(agent.model, "gpt-4o-mini")
        # The exact production failure mode: an untrimmed value must
        # never survive far enough to become an "Illegal header value".
        self.assertNotIn("\n", agent.api_key)
        self.assertNotIn("\n", agent.model)

    def test_still_raises_for_genuinely_empty_key(self):
        from agents.openai_agent import OpenAIAgent

        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "   ", "OPENAI_MODEL": "gpt-4o-mini"}):
            with self.assertRaises(ValueError):
                OpenAIAgent()

    def test_still_raises_for_genuinely_empty_model(self):
        from agents.openai_agent import OpenAIAgent

        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-real-key", "OPENAI_MODEL": "  "}):
            with self.assertRaises(ValueError):
                OpenAIAgent()

    def test_ordinary_untrimmed_key_unaffected(self):
        from agents.openai_agent import OpenAIAgent

        with mock.patch.dict(
            os.environ, {"OPENAI_API_KEY": "sk-real-key", "OPENAI_MODEL": "gpt-4o-mini"}
        ):
            agent = OpenAIAgent()
        self.assertEqual(agent.api_key, "sk-real-key")
        self.assertEqual(agent.model, "gpt-4o-mini")


if __name__ == "__main__":
    unittest.main()
