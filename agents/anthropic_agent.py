import os
import httpx


class AnthropicAgent:
    def __init__(self):
        self.api_key = os.getenv("ANTHROPIC_API_KEY")
        self.model = os.getenv("ANTHROPIC_MODEL")

        if not self.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is missing")

        if not self.model:
            raise RuntimeError("ANTHROPIC_MODEL is missing")

    async def run(self, prompt: str) -> str:
        print("=== ANTHROPIC REQUEST START ===")
        print("MODEL:", self.model)

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        payload = {
            "model": self.model,
            "max_tokens": 2048,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
        }

        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json=payload,
            )

        print("STATUS:", response.status_code)
        print("BODY:", response.text)

        if response.status_code != 200:
            raise RuntimeError(
                f"Anthropic API error {response.status_code}: {response.text}"
            )

        data = response.json()

        result = []

        for item in data.get("content", []):
            if item.get("type") == "text":
                result.append(item.get("text", ""))

        return "\n".join(result).strip()
