import os
import httpx


class AnthropicAgent:
    def __init__(self):
        self.api_key = os.getenv("ANTHROPIC_API_KEY")
        self.model = os.getenv("ANTHROPIC_MODEL")

        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY not found")

        if not self.model:
            raise ValueError("ANTHROPIC_MODEL not found")

    async def run(self, prompt: str) -> str:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self.model,
                    "max_tokens": 2048,
                    "messages": [
                        {
                            "role": "user",
                            "content": prompt,
                        }
                    ],
                },
            )

            if response.status_code != 200:
    print(response.status_code)
    print(response.text)
    raise RuntimeError(response.text)

            data = response.json()

            result = []

            for block in data.get("content", []):
                if block.get("type") == "text":
                    result.append(block.get("text", ""))

            return "\n".join(result).strip()
