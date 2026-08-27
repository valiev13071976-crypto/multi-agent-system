import os
import uuid
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from dotenv import load_dotenv

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from agents.router import Router
from agents.model_router import (
    BudgetRoutingDeniedError,
    NoCapableProviderError,
    ProviderCapabilityMismatchError,
)
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
from security.api_auth import (
    configure_security,
    get_audit_log,
    get_resource_authorizer,
    get_security_context,
)
from security.errors import ResourceNotFoundError, UnauthorizedError
from security.identity import RequestSecurityContext
from security.rbac import (
    PERM_ANALYZE_EXECUTE,
    PERM_WORKFLOW_CANCEL,
    PERM_WORKFLOW_CREATE,
    PERM_WORKFLOW_READ,
    PERM_WORKFLOW_RESUME,
)
from security.redaction import redact
from security.config import cors_allow_origins
from security.request_limits import RequestSizeLimitMiddleware
from security.secrets import EnvSecretStore
from side_effects.runtime import compose_side_effect_runtime

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
configure_security()
# Production auto-wiring: share composed workflow/HITL/persistence with analyze engine.
if side_effect_runtime.workflow_engine is not None:
    router.workflow_engine = side_effect_runtime.workflow_engine

# Durable DAG platform + TaskQueue (long-running). Analyze stays sync.
workflow_runtime = getattr(side_effect_runtime, "workflow_runtime", None)
if workflow_runtime is not None:
    from workflow.builtins import register_builtin_definitions
    from workflow.definition import StepResult, STEP_TYPE_HANDLER, STEP_TYPE_BRANCH

    register_builtin_definitions(workflow_runtime.definitions)

    async def _default_handler(ctx):
        step = ctx["step"]
        return StepResult(ok=True, data={"step_id": step.step_id, "path": "left"})

    workflow_runtime.platform.register_handler(STEP_TYPE_HANDLER, _default_handler)
    workflow_runtime.platform.register_handler(STEP_TYPE_BRANCH, _default_handler)


class WorkflowCreateRequest(BaseModel):
    workflow_type: str = Field(..., min_length=1, max_length=200)
    version: str = Field(default="1", min_length=1, max_length=64)
    sync: bool = Field(
        default=False,
        description="If true, run inline (short workflows). If false, enqueue to TaskQueue.",
    )
    metadata: dict = Field(default_factory=dict)


class WorkflowStatusResponse(BaseModel):
    workflow_id: str
    workflow_type: str = ""
    version: str = ""
    status: str
    ready_steps: list = Field(default_factory=list)
    current_steps: list = Field(default_factory=list)
    waiting: bool = False
    progress: dict = Field(default_factory=dict)
    steps: list = Field(default_factory=list)
    error_code: str | None = None
    next_retry_at: str | None = None
    deadline_at: str | None = None
    queue_task_id: str | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Read-only repo probe if GITHUB_WRITE_PROBE_ON_STARTUP=true. No writes.

    Starts background TaskQueue worker for durable workflows when enabled.
    """
    await side_effect_runtime.start()
    try:
        yield
    finally:
        wr = getattr(side_effect_runtime, "workflow_runtime", None)
        if wr is not None:
            try:
                await wr.stop_background()
            except Exception:
                pass


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

app.add_middleware(RequestSizeLimitMiddleware)

_origins = cors_allow_origins()
if _origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "X-API-Key", "Content-Type"],
    )


def _authorize_workflow(
    ctx: RequestSecurityContext, workflow_id: str, permission: str
) -> None:
    authorizer = get_resource_authorizer()
    audit = get_audit_log()
    try:
        authorizer.require_permission(ctx, permission)
        state = router.workflow_engine.state_manager.get(workflow_id)
        authorizer.authorize_workflow_access(ctx, state, permission=permission)
    except ResourceNotFoundError:
        audit.record(
            "authz.denied",
            actor_ref=ctx.actor_ref(),
            tenant_ref=ctx.tenant_id,
            resource_ref=workflow_id,
            outcome="denied",
            reason_code="not_found",
        )
        raise HTTPException(status_code=404, detail={"error": "workflow_not_found"})
    except UnauthorizedError as exc:
        audit.record(
            "authz.denied",
            actor_ref=ctx.actor_ref(),
            tenant_ref=ctx.tenant_id,
            resource_ref=workflow_id,
            outcome="denied",
            reason_code=getattr(exc, "error_code", "unauthorized"),
        )
        raise HTTPException(status_code=403, detail={"error": "unauthorized"})


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
async def analyze(
    request: AnalyzeRequest,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):

    try:
        get_resource_authorizer().require_permission(ctx, PERM_ANALYZE_EXECUTE)
        get_audit_log().record(
            "analyze.requested",
            actor_ref=ctx.actor_ref(),
            tenant_ref=ctx.tenant_id,
            outcome="ok",
        )
        task_id = str(uuid.uuid4())
        result = await router.workflow_engine.execute(
            request.prompt,
            request.mode,
            request.role,
            context_manager=context_manager,
            run_router=router.run,
            task_id=task_id,
            tenant_id=ctx.tenant_id,
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

    except BudgetRoutingDeniedError as e:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "finops_budget_denied",
                "message": redact("Request blocked by FinOps budget policy."),
                "reason": redact(str(e.reason)),
                "provider": e.provider,
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


@app.post("/api/workflows", response_model=WorkflowStatusResponse)
async def create_workflow(
    request: WorkflowCreateRequest,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    if workflow_runtime is None:
        raise HTTPException(
            status_code=503,
            detail={"error": "workflow_runtime_unavailable"},
        )
    try:
        get_resource_authorizer().require_permission(ctx, PERM_WORKFLOW_CREATE)
        if request.sync:
            result = await workflow_runtime.create_and_run_sync(
                request.workflow_type,
                request.version,
                metadata=request.metadata,
                tenant_id=ctx.tenant_id,
            )
            return WorkflowStatusResponse(**{
                k: result.get(k)
                for k in WorkflowStatusResponse.model_fields
                if k in result
            })
        created = await workflow_runtime.create_and_enqueue(
            request.workflow_type,
            request.version,
            metadata=request.metadata,
            tenant_id=ctx.tenant_id,
        )
        status = workflow_runtime.get_status(created["workflow_id"])
        status["queue_task_id"] = created.get("queue_task_id")
        return WorkflowStatusResponse(**{
            k: status.get(k)
            for k in WorkflowStatusResponse.model_fields
            if k in status or k == "queue_task_id"
        })
    except Exception as exc:
        code = getattr(exc, "error_code", None) or type(exc).__name__
        status = 400 if code in {
            "definition_not_found",
            "unknown_dependency",
            "cycle_detected",
            "empty_definition",
        } else 500
        raise HTTPException(
            status_code=status,
            detail={"error": redact(str(code)), "message": redact(str(exc))},
        )


@app.get("/api/workflows/{workflow_id}", response_model=WorkflowStatusResponse)
async def get_workflow(
    workflow_id: str,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    if workflow_runtime is None:
        raise HTTPException(
            status_code=503,
            detail={"error": "workflow_runtime_unavailable"},
        )
    try:
        _authorize_workflow(ctx, workflow_id, PERM_WORKFLOW_READ)
        status = workflow_runtime.get_status(workflow_id)
        return WorkflowStatusResponse(**status)
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "workflow_not_found", "message": redact(str(exc))},
        )


@app.post("/api/workflows/{workflow_id}/cancel", response_model=WorkflowStatusResponse)
async def cancel_workflow(
    workflow_id: str,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    if workflow_runtime is None:
        raise HTTPException(
            status_code=503,
            detail={"error": "workflow_runtime_unavailable"},
        )
    try:
        _authorize_workflow(ctx, workflow_id, PERM_WORKFLOW_CANCEL)
        workflow_runtime.cancel(workflow_id)
        get_audit_log().record(
            "workflow.cancelled",
            actor_ref=ctx.actor_ref(),
            tenant_ref=ctx.tenant_id,
            resource_ref=workflow_id,
            outcome="ok",
        )
        return WorkflowStatusResponse(**workflow_runtime.get_status(workflow_id))
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "workflow_not_found", "message": redact(str(exc))},
        )


@app.post("/api/workflows/{workflow_id}/resume")
async def resume_workflow(
    workflow_id: str,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
):
    """Re-queue after HITL approval (does not bypass AutonomyGate/HITL)."""
    if workflow_runtime is None:
        raise HTTPException(
            status_code=503,
            detail={"error": "workflow_runtime_unavailable"},
        )
    try:
        _authorize_workflow(ctx, workflow_id, PERM_WORKFLOW_RESUME)
        result = await workflow_runtime.resume_after_approval(workflow_id)
        get_audit_log().record(
            "workflow.resumed",
            actor_ref=ctx.actor_ref(),
            tenant_ref=ctx.tenant_id,
            resource_ref=workflow_id,
            outcome="ok",
        )
        return result
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "resume_failed", "message": redact(str(exc))},
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
