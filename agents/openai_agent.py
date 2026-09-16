import os
import httpx


class OpenAIAgent:
    def __init__(self):
        # Deployment/secret-injection pipelines (e.g. Cloud Agent secrets)
        # can append a trailing newline/whitespace to an injected env var
        # without that being visible in any UI. An untrimmed value here
        # becomes an "Illegal header value" the underlying HTTP client
        # (httpx) rejects outright before ever reaching the network,
        # turning a perfectly valid key into a hard technical failure of
        # this whole boundary. Several other credential readers in this
        # repo already ``.strip()`` for the exact same reason (see e.g.
        # ``integrations/production/adapters/speech.py``,
        # ``production_validation/providers_live.py``) -- this is the
        # ONE shared model-call seam ``data_intel.nl_plan_llm``/
        # ``managed_agent_poc`` both rely on, so it gets the same
        # defensive treatment.
        self.api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
        self.model = (os.getenv("OPENAI_MODEL") or "").strip()

        if not self.api_key:
            raise ValueError("OPENAI_API_KEY not found")

        if not self.model:
            raise ValueError("OPENAI_MODEL not found")

    async def run(self, prompt: str) -> str:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                "https://api.openai.com/v1/responses",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "input": prompt,
                },
            )

            response.raise_for_status()

            data = response.json()

            if data.get("output_text"):
                text = data["output_text"]
            else:
                result = []
                for item in data.get("output", []):
                    for content in item.get("content", []):
                        if content.get("type") == "output_text":
                            result.append(content.get("text", ""))
                        elif content.get("text"):
                            result.append(content["text"])
                text = "\n".join(result).strip()

            from agents.provider_result import usage_from_openai_response
            return usage_from_openai_response(
                data,
                provider_id="openai",
                model_id=self.model,
                text=text,
            )
