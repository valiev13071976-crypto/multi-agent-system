import os
import httpx


class GrokAgent:
    def __init__(self):
        self.api_key = os.getenv("XAI_API_KEY")
        self.model = os.getenv("XAI_MODEL")

        if not self.api_key:
            raise RuntimeError("XAI_API_KEY is missing")

        if not self.model:
            raise RuntimeError("XAI_MODEL is missing")

    async def run(self, prompt: str) -> str:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                "https://api.x.ai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": [
                        {
                            "role": "user",
                            "content": prompt,
                        }
                    ],
                    "temperature": 0.7,
                },
            )

        if response.status_code != 200:
            raise RuntimeError(
                f"Grok API error {response.status_code}"
            )

        data = response.json()
        text = data["choices"][0]["message"]["content"]

        from agents.provider_result import usage_from_chat_completions_response
        return usage_from_chat_completions_response(
            data,
            provider_id="grok",
            model_id=self.model,
            text=text,
        )
