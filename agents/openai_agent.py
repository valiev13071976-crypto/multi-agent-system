import os
import httpx


class OpenAIAgent:
    def __init__(self):
        self.api_key = os.getenv("OPENAI_API_KEY")
        self.model = os.getenv("OPENAI_MODEL")

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
                return data["output_text"]

            result = []

            for item in data.get("output", []):
                for content in item.get("content", []):
                    if content.get("type") == "output_text":
                        result.append(content.get("text", ""))

                    elif content.get("text"):
                        result.append(content["text"])

            return "\n".join(result).strip()
