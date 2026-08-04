import os
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from agents.router import Router
from agents.context_manager import ContextManager


PUBLIC_URL = "https://multi-agent-system-production-8d0c.up.railway.app"


app = FastAPI(
    title="Panda Multi-Agent",
    description="API мультиагентной системы Panda.",
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
    )

    mode: Literal[
        "openai",
        "anthropic",
        "gemini",
        "grok",
        "deepseek",
        "both",
    ] = "both"


class HealthResponse(BaseModel):
    status: Literal["ok"]
    openai_configured: bool
    anthropic_configured: bool


class AnalyzeResponse(BaseModel):
    mode: str

    strategist: str | None = None
    critic: str | None = None
    researcher: str | None = None
    technical: str | None = None
    judge: str | None = None

    openai: str | None = None
    anthropic: str | None = None
    gemini: str | None = None
    grok: str | None = None
    deepseek: str | None = None

    errors: dict | None = None



router = Router()

context_manager = ContextManager()



@app.get(
    "/health",
    response_model=HealthResponse,
)
async def health():

    return HealthResponse(
        status="ok",
        openai_configured=bool(
            os.getenv("OPENAI_API_KEY")
        ),
        anthropic_configured=bool(
            os.getenv("ANTHROPIC_API_KEY")
        ),
    )



@app.post(
    "/api/analyze",
    response_model=AnalyzeResponse,
)
async def analyze(request: AnalyzeRequest):

    try:

        prepared_context = await context_manager.prepare(
            request.prompt
        )

        result = await router.run(
            prompt=str(prepared_context),
            mode=request.mode,
        )


        return AnalyzeResponse(
    mode=result.get("model", request.mode),

    strategist=result.get("strategist"),
    critic=result.get("critic"),
    researcher=result.get("researcher"),
    technical=result.get("technical"),
    judge=result.get("judge"),

    openai=result.get("openai"),
    anthropic=result.get("anthropic"),
    gemini=result.get("gemini"),
    grok=result.get("grok"),
    deepseek=result.get("deepseek"),

    errors=result.get("errors"),
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
    <option value="both">OpenAI + Anthropic + Gemini + Grok</option>
    <option value="openai">Только OpenAI</option>
    <option value="anthropic">Только Anthropic</option>
    <option value="gemini">Только Gemini</option>
    <option value="grok">Только Grok</option>
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
