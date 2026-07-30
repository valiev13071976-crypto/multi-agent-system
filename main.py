import asyncio
import os
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field


app = FastAPI(title="Panda Multi-Agent", version="0.1.0")


class AnalyzeRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=30000)
    mode: Literal["openai", "anthropic", "both"] = "both"


def extract_openai_text(payload: dict[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    parts: list[str] = []
    for item in payload.get("output", []):
        for content in item.get("content", []):
            text = content.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return "\n".join(parts).strip()


async def ask_openai(prompt: str) -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL")
    if not api_key or not model:
        raise RuntimeError("OPENAI_API_KEY or OPENAI_MODEL is not configured")

    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "input": prompt,
            },
        )
        response.raise_for_status()
        text = extract_openai_text(response.json())
        if not text:
            raise RuntimeError("OpenAI returned an empty response")
        return text


async def ask_anthropic(prompt: str) -> str:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    model = os.getenv("ANTHROPIC_MODEL")
    if not api_key or not model:
        raise RuntimeError("ANTHROPIC_API_KEY or ANTHROPIC_MODEL is not configured")

    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 2048,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        response.raise_for_status()
        payload = response.json()
        parts = [
            block.get("text", "")
            for block in payload.get("content", [])
            if block.get("type") == "text"
        ]
        text = "\n".join(part for part in parts if part).strip()
        if not text:
            raise RuntimeError("Anthropic returned an empty response")
        return text


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "openai_configured": bool(os.getenv("OPENAI_API_KEY") and os.getenv("OPENAI_MODEL")),
        "anthropic_configured": bool(os.getenv("ANTHROPIC_API_KEY") and os.getenv("ANTHROPIC_MODEL")),
    }


@app.post("/api/analyze")
async def analyze(request: AnalyzeRequest) -> dict[str, Any]:
    try:
        if request.mode == "openai":
            return {"mode": "openai", "openai": await ask_openai(request.prompt)}

        if request.mode == "anthropic":
            return {"mode": "anthropic", "anthropic": await ask_anthropic(request.prompt)}

        results = await asyncio.gather(
            ask_openai(request.prompt),
            ask_anthropic(request.prompt),
            return_exceptions=True,
        )

        response: dict[str, Any] = {"mode": "both"}
        errors: dict[str, str] = {}

        if isinstance(results[0], Exception):
            errors["openai"] = str(results[0])
        else:
            response["openai"] = results[0]

        if isinstance(results[1], Exception):
            errors["anthropic"] = str(results[1])
        else:
            response["anthropic"] = results[1]

        if errors:
            response["errors"] = errors

        if "openai" not in response and "anthropic" not in response:
            raise HTTPException(status_code=502, detail=response["errors"])

        return response

    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:1000]
        raise HTTPException(
            status_code=502,
            detail=f"Provider error {exc.response.status_code}: {body}",
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/", response_class=HTMLResponse)
async def home() -> str:
    return """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Panda Multi-Agent</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 920px; margin: 40px auto; padding: 0 18px; background:#f5f5f5; }
    .card { background:white; border-radius:18px; padding:24px; box-shadow:0 8px 30px rgba(0,0,0,.08); }
    textarea { width:100%; min-height:180px; box-sizing:border-box; padding:14px; font:inherit; }
    select, button { padding:12px 16px; margin-top:12px; font:inherit; }
    button { cursor:pointer; }
    pre { white-space:pre-wrap; background:#111; color:#eee; padding:16px; border-radius:12px; min-height:100px; }
  </style>
</head>
<body>
  <div class="card">
    <h1>Panda Multi-Agent</h1>
    <p>Первая версия: отправляет одну задачу OpenAI, Anthropic или обоим параллельно.</p>
    <textarea id="prompt" placeholder="Введите задачу..."></textarea>
    <div>
      <select id="mode">
        <option value="both">OpenAI + Anthropic</option>
        <option value="openai">Только OpenAI</option>
        <option value="anthropic">Только Anthropic</option>
      </select>
      <button onclick="run()">Запустить анализ</button>
    </div>
    <h3>Результат</h3>
    <pre id="result">Готово к запросу.</pre>
  </div>
<script>
async function run() {
  const result = document.getElementById('result');
  result.textContent = 'Выполняется...';
  try {
    const response = await fetch('/api/analyze', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        prompt: document.getElementById('prompt').value,
        mode: document.getElementById('mode').value
      })
    });
    const data = await response.json();
    result.textContent = JSON.stringify(data, null, 2);
  } catch (error) {
    result.textContent = String(error);
  }
}
</script>
</body>
</html>
"""


