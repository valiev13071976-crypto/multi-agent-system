import os
import httpx


class DeepSeekAgent:
    def __init__(self):
        self.api_key = os.getenv("DEEPSEEK_API_KEY")
        self.model = os.getenv("DEEPSEEK_MODEL")

        if not self.api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is missing")

        if not self.model:
            raise RuntimeError("DEEPSEEK_MODEL is missing")

    async def run(self, prompt: str) -> str:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                "https://api.deepseek.com/chat/completions",
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
                f"DeepSeek API error {response.status_code}"
            )

        data = response.json()
        text = data["choices"][0]["message"]["content"]

        from agents.provider_result import usage_from_chat_completions_response
        return usage_from_chat_completions_response(
            data,
            provider_id="deepseek",
            model_id=self.model,
            text=text,
        )
