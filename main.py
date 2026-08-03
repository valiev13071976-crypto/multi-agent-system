import asyncio
import os
from typing import Any, Literal
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from agents.openai_agent import OpenAIAgent
from agents.anthropic_agent import AnthropicAgent
from agents.gemini_agent import GeminiAgent
from agents.router import Router


PUBLIC_URL = "https://multi-agent-system-production-8d0c.up.railway.app"

app = FastAPI(
    title="Panda Multi-Agent",
    description="API для отправки запросов моделям OpenAI и Anthropic.",
    version="1.0.0",
    servers=[
        {
            "url": PUBLIC_URL,
            "description": "Railway production server",
        }
    ],
)


class AnalyzeRequest(BaseModel):
    prompt: str = Field(
        ...,
        min_length=1,
        max_length=30000,
        description="Задача или вопрос для моделей.",
        examples=["Проанализируй преимущества солнечной энергетики."],
    )
    mode: Literal["openai", "anthropic", "both"] = Field(
        default="both",
        description="Какие модели использовать.",
    )


class HealthResponse(BaseModel):
    status: Literal["ok"]
    openai_configured: bool
    anthropic_configured: bool


class AnalyzeResponse(BaseModel):
    mode: Literal[
        "openai",
        "anthropic",
        "gemini",
        "both",
    ]

    openai: str | None = None
    anthropic: str | None = None
    gemini: str | None = None
    errors: dict[str, str] | None = None


def extract_openai_text(payload: dict[str, Any]) -> str:
    direct = payload.get("output_text")

    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    parts: list[str] = []

    for item in payload.get("output", []):
        if not isinstance(item, dict):
            continue

        for content in item.get("content", []):
            if not isinstance(content, dict):
                continue

            text = content.get("text")

            if isinstance(text, str) and text.strip():
                parts.append(text.strip())

    return "\n".join(parts).strip()


async def ask_openai(prompt: str) -> str:
    agent = OpenAIAgent()
    return await agent.run(prompt)


async def ask_anthropic(prompt: str) -> str:
    agent = AnthropicAgent()
    return await agent.run(prompt)


async def ask_gemini(prompt: str) -> str:
    agent = GeminiAgent()
    return await agent.run(prompt)


router = Router()
    


@app.get(
    "/health",
    response_model=HealthResponse,
    operation_id="checkHealth",
    summary="Проверить состояние сервера",
)
async def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        openai_configured=bool(
            os.getenv("OPENAI_API_KEY") and os.getenv("OPENAI_MODEL")
        ),
        anthropic_configured=bool(
            os.getenv("ANTHROPIC_API_KEY") and os.getenv("ANTHROPIC_MODEL")
        ),
    )


@app.post(
    "/api/analyze",
    response_model=AnalyzeResponse,
    response_model_exclude_none=True,
    operation_id="analyzeWithModels",
    summary="Отправить запрос моделям",
    description=(
        "Отправляет запрос OpenAI, Anthropic или обеим моделям параллельно."
    ),
)
@app.post(
    "/api/analyze",
    response_model=AnalyzeResponse,
    response_model_exclude_none=True,
)
async def analyze(request: AnalyzeRequest):

    try:
        result = await router.run(
            prompt=request.prompt,
            mode=request.mode,
        )

        return AnalyzeResponse(
    mode=result["model"],
    openai=result.get("openai"),
    anthropic=result.get("anthropic"),
    gemini=result.get("gemini"),
)
        

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=str(e),
        )


@app.get(
    "/",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def home() -> str:
    return """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Panda Multi-Agent</title>
  <style>
    body {
      font-family: system-ui, sans-serif;
      max-width: 920px;
      margin: 40px auto;
      padding: 0 18px;
      background: #f5f5f5;
    }

    .card {
      background: white;
      border-radius: 18px;
      padding: 24px;
      box-shadow: 0 8px 30px rgba(0, 0, 0, .08);
    }

    textarea {
      width: 100%;
      min-height: 180px;
      box-sizing: border-box;
      padding: 14px;
      font: inherit;
    }

    select,
    button {
      padding: 12px 16px;
      margin-top: 12px;
      font: inherit;
    }

    button {
      cursor: pointer;
    }

    pre {
      white-space: pre-wrap;
      background: #111;
      color: #eee;
      padding: 16px;
      border-radius: 12px;
      min-height: 100px;
    }
  </style>
</head>
<body>
  <div class="card">
    <h1>TEST 123456</h1>

    <p>
      Отправляет одну задачу OpenAI, Anthropic
      или обеим моделям параллельно.
    </p>

    <textarea
      id="prompt"
      placeholder="Введите задачу..."
    ></textarea>

    <div>
      <select id="mode">
    <option value="both">OpenAI + Anthropic + Gemini</option>
    <option value="openai">Только OpenAI</option>
    <option value="anthropic">Только Anthropic</option>
    <option value="gemini">Только Gemini</option>
</select>

      <button onclick="run()">Запустить анализ</button>
    </div>

    <h3>Результат</h3>

    <pre id="result">Готово к запросу.</pre>
  </div>

<script>
async function run() {
  const result = document.getElementById("result");
  const prompt = document.getElementById("prompt").value;
  const mode = document.getElementById("mode").value;

  result.textContent = "Выполняется...";

  try {
    const response = await fetch("/api/analyze", {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        prompt: prompt,
        mode: mode
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
