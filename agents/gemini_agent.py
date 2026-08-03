import os
import httpx


class GeminiAgent:
    def __init__(self):
        self.api_key = os.getenv("GEMINI_API_KEY")
        self.model = os.getenv("GEMINI_MODEL")

        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY is missing")

        if not self.model:
            raise RuntimeError("GEMINI_MODEL is missing")

    async def run(self, prompt: str) -> str:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent?key={self.api_key}"
        )

        payload = {
            "contents": [
                {
                    "parts": [
                        {
                            "text": prompt
                        }
                    ]
                }
            ]
        }

        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                url,
                json=payload,
            )

        if response.status_code != 200:
            raise RuntimeError(
                f"Gemini API error {response.status_code}: {response.text}"
            )

        data = response.json()

        return (
            data["candidates"][0]
            ["content"]["parts"][0]
            ["text"]
        )
