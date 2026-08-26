import os
from contextlib import asynccontextmanager
from typing import Literal

from dotenv import load_dotenv

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from agents.router import Router
from agents.model_router import NoCapableProviderError, ProviderCapabilityMismatchError
from agents.router_v2 import (
    ALLOWED_API_ROLE_VALUES,
    ALLOWED_MODE_VALUES,
    InvalidModeError,
    NoProvidersAvailableError,
    ProviderNotConfiguredError,
    RouterV2,
)
from agents.core.expert_manager import FinOpsBudgetDeniedError
from agents.role_registry import (
    DEFAULT_ROLE,
    InvalidRoleError,
)
from agents.context_manager import ContextManager
from security.redaction import redact
from security.secrets import EnvSecretStore
from side_effects.runtime import compose_side_effect_runtime
import uuid

load_dotenv()

PUBLIC_URL = "https://multi-agent-system-production-8d0c.up.railway.app"


class AnalyzeRequest(BaseModel):
    prompt: str = Field(
        ...,
        min_length=1,
        max_length=30000,
        description="Задача или вопрос для моделей.",
    )

    mode: str = Field(
        default="both",
        json_schema_extra={
            "enum": list(ALLOWED_MODE_VALUES),
        },
        description="Какой LLM provider вызывать.",
    )

    role: str = Field(
        default=DEFAULT_ROLE,
        json_schema_extra={
            "enum": list(ALLOWED_API_ROLE_VALUES),
        },
        description="Какую expert role instruction использовать.",
    )


class HealthResponse(BaseModel):
    status: Literal["ok"]
    providers: dict[str, bool]


class AnalyzeResponse(BaseModel):
    summary: str
    best_solution: str
    analysis: str
    risks: list
    action_plan: list
    confidence: int | float
    role: str



USE_V2 = True

router = RouterV2() if USE_V2 else Router()

context_manager = ContextManager()

side_effect_runtime = compose_side_effect_runtime(
    secrets=EnvSecretStore(), isolate_errors=True
)
# Production auto-wiring: share composed workflow/HITL/persistence with analyze engine.
if side_effect_runtime.workflow_engine is not None:
    router.workflow_engine = side_effect_runtime.workflow_engine


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Read-only repo probe if GITHUB_WRITE_PROBE_ON_STARTUP=true. No writes."""
    await side_effect_runtime.start()
    yield


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
    lifespan=lifespan,
)


@app.get(
    "/health",
    response_model=HealthResponse,
)
async def health():

    return HealthResponse(
        status="ok",
        providers=router.provider_status(),
    )



@app.post(
    "/api/analyze",
    response_model=AnalyzeResponse,
)
async def analyze(request: AnalyzeRequest):

    try:

        task_id = str(uuid.uuid4())
        result = await router.workflow_engine.execute(
            request.prompt,
            request.mode,
            request.role,
            context_manager=context_manager,
            run_router=router.run,
            task_id=task_id,
        )
        router.last_task_id = task_id
        router.last_workflow_id = router.workflow_engine.last_workflow_id

        return AnalyzeResponse(
            summary=result.get("summary", ""),
            best_solution=result.get("best_solution", ""),
            analysis=result.get("analysis", ""),
            risks=result.get("risks", []),
            action_plan=result.get("action_plan", []),
            confidence=result.get("confidence", 0),
            role=result.get("role", "Judge"),
        )

    except HTTPException:
        raise

    except InvalidModeError as e:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_mode",
                "message": "Unknown analyze mode.",
                "mode": e.mode,
            },
        )

    except InvalidRoleError as e:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_role",
                "message": "Unknown analyze role.",
                "role": e.role,
            },
        )

    except ProviderNotConfiguredError as e:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "provider_not_configured",
                "message": "The selected LLM provider is not configured.",
                "mode": e.mode,
                "provider": e.provider,
            },
        )

    except NoProvidersAvailableError:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "no_providers_available",
                "message": "No LLM providers are configured.",
            },
        )

    except NoCapableProviderError as e:
        detail = {
            "error": "no_capable_provider",
            "message": "No configured provider supports the requested task category.",
            "category": e.category,
        }
        if getattr(e, "reason", None) == "requirements":
            detail["message"] = (
                "No configured provider satisfies the required model capabilities."
            )
            detail["reason"] = "requirements"
            detail["missing_capabilities"] = list(
                getattr(e, "missing_capabilities", ()) or ()
            )
        raise HTTPException(status_code=503, detail=detail)

    except ProviderCapabilityMismatchError as e:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "provider_capability_mismatch",
                "message": "Selected provider does not satisfy required model capabilities.",
                "provider": e.provider,
                "missing_capabilities": list(e.missing_capabilities),
                "category": e.category,
            },
        )

    except FinOpsBudgetDeniedError as e:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "finops_budget_denied",
                "message": redact("Request blocked by FinOps budget policy."),
                "reason": redact(str(e.reason)),
            },
        )

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=redact(str(e)),
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
