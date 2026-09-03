import os
import uuid
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from dotenv import load_dotenv

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
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
from security.tenant import MissingTenantError
from security.api_auth import (
    PublicRateLimitMiddleware,
    configure_security,
    get_audit_log,
    get_resource_authorizer,
    get_security_context,
)
from security.errors import ResourceNotFoundError, UnauthorizedError
from security.hitl_auth import HitlActionPayload, HitlHttpAuthorizer
from security.identity import RequestSecurityContext
from workflow.errors import WorkflowNotFoundError
from security.rbac import (
    PERM_ADMIN_METADATA,
    PERM_ANALYZE_EXECUTE,
    PERM_HITL_APPROVE,
    PERM_OPS_WRITE,
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

# Canonical production storage paths (Stage 1).
try:
    from production_foundation.storage import resolve_store_paths

    for key, value in resolve_store_paths().items():
        if value and not os.environ.get(key):
            os.environ[key] = value
except Exception:
    pass

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


class ReadyResponse(BaseModel):
    liveness: str
    readiness: str
    role: str
    draining: bool = False
    dependencies: list[dict] = []
    capabilities: dict = {}


class DrainResponse(BaseModel):
    draining: bool
    readiness: str


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

# Fail-fast production runtime config (profiles + capacity/lease invariants).
try:
    from config.runtime_config import validate_runtime_config

    _RUNTIME_CONFIG = validate_runtime_config(raise_on_error=True)
except Exception:
    # Allow import of main for tooling when env is intentionally incomplete;
    # HTTP readiness will still surface config errors.
    from config.runtime_config import validate_runtime_config

    _RUNTIME_CONFIG = validate_runtime_config(raise_on_error=False)

# Shared durable FinOps budget when SQLite persistence is ready (multi-replica safe).
if (
    USE_V2
    and getattr(router, "budget_guard", None) is not None
    and side_effect_runtime.persistence is not None
    and side_effect_runtime.persistence.ready
    and side_effect_runtime.persistence.backend == "sqlite"
    and side_effect_runtime.persistence.database_path_ref
):
    try:
        from finops.budget_store import SqliteBudgetStore

        db_path = os.environ.get("SIDE_EFFECT_DB_PATH") or os.environ.get(
            "FINOPS_BUDGET_DB_PATH"
        )
        if db_path:
            budget_store = SqliteBudgetStore(db_path)
            router.budget_guard.store = budget_store
            router.budget_guard.ledger.store = budget_store
            if getattr(router, "pipeline", None) is not None:
                em = getattr(router.pipeline, "expert_manager", None)
                if em is not None and getattr(em, "budget_guard", None) is not None:
                    em.budget_guard.store = budget_store
                    em.budget_guard.ledger.store = budget_store
    except Exception:
        pass
# Shared provider governor (capacity ≠ health) across API/worker replicas.
if (
    USE_V2
    and side_effect_runtime.persistence is not None
    and side_effect_runtime.persistence.ready
    and side_effect_runtime.persistence.backend == "sqlite"
):
    try:
        from providers.governor import (
            GovernorLimits,
            ProviderGovernor,
            SqliteProviderGovernorStore,
        )

        db_path = os.environ.get("SIDE_EFFECT_DB_PATH") or os.environ.get(
            "PROVIDER_GOVERNOR_DB_PATH"
        )
        if db_path:
            gov_limits = GovernorLimits.from_env()
            gov_store = SqliteProviderGovernorStore(db_path, gov_limits)
            governor = ProviderGovernor(
                store=gov_store,
                limits=gov_limits,
                observability=getattr(side_effect_runtime, "observability", None),
            )
            router.provider_governor = governor
            if getattr(router, "model_router", None) is not None:
                router.model_router.capacity_governor = governor
            if getattr(router, "pipeline", None) is not None:
                em = getattr(router.pipeline, "expert_manager", None)
                if em is not None:
                    em.provider_governor = governor
    except Exception:
        pass
# Production auto-wiring: share composed workflow/HITL/persistence with analyze engine.
if side_effect_runtime.workflow_engine is not None:
    router.workflow_engine = side_effect_runtime.workflow_engine

# Durable DAG platform + TaskQueue (long-running). Analyze stays sync.
workflow_runtime = getattr(side_effect_runtime, "workflow_runtime", None)
if workflow_runtime is not None:
    from workflow.builtins import register_builtin_definitions
    from workflow.definition import StepResult, STEP_TYPE_HANDLER, STEP_TYPE_BRANCH

    register_builtin_definitions(workflow_runtime.definitions)
    try:
        from documents.intelligence.workflow_def import register_document_workflows

        register_document_workflows(
            workflow_runtime.definitions, workflow_runtime.platform
        )
    except Exception:
        pass
    try:
        from data_intel.workflow_def import register_data_intel_workflows

        register_data_intel_workflows(
            workflow_runtime.definitions, workflow_runtime.platform
        )
    except Exception:
        pass

    async def _default_handler(ctx):
        step = ctx["step"]
        return StepResult(ok=True, data={"step_id": step.step_id, "path": "left"})

    # Default handler for demo step_types; document/data handlers registered by step_id
    workflow_runtime.platform.register_handler(STEP_TYPE_HANDLER, _default_handler)
    workflow_runtime.platform.register_handler(STEP_TYPE_BRANCH, _default_handler)

from business_assistant_api.runtime import build_business_assistant_api_runtime
from business_assistant_api.router import configure_business_assistant_api_router
from telegram_interface.config import telegram_interface_enabled
from telegram_interface.runtime import build_telegram_interface_runtime
from telegram_interface.router import configure_telegram_interface_router
from voice_interface.config import voice_interface_enabled
from voice_interface.runtime import build_voice_interface_runtime
from voice_interface.router import configure_voice_interface_router
from ui_chat.runtime import build_ui_chat_runtime
from ui_chat.router import configure_ui_chat_router
from operations_admin.runtime import build_operations_admin_runtime
from operations_admin.router import configure_operations_admin_router
from analytics_dashboard.runtime import build_analytics_dashboard_runtime
from analytics_dashboard.router import configure_analytics_dashboard_router
from scheduled_automation.runtime import build_scheduled_automation_runtime
from scheduled_automation.router import configure_scheduled_automation_router
from controlled_automation.runtime import build_controlled_automation_runtime
from controlled_automation.router import configure_controlled_automation_router
from scale_optimization.runtime import build_scale_optimization_runtime
from scale_optimization.router import configure_scale_optimization_router
from saas_product.runtime import build_saas_product_runtime
from saas_product.router import configure_saas_product_router
from saas_product.deployment import assert_production_safe
from accounts.runtime import build_accounts_runtime
from accounts.router import configure_accounts_router
from accounts.dual_auth import configure_accounts_auth, install_dual_auth
from production_foundation.runtime import initialize_production_foundation
from integrations.production.runtime import build_production_integration_runtime
from integrations.production.router import configure_production_integration_router

production_integration_runtime = build_production_integration_runtime(
    health_tracker=getattr(router, "health_tracker", None),
)
_production_bundle = production_integration_runtime.bundle

ui_chat_runtime = build_ui_chat_runtime(
    side_effect_runtime=side_effect_runtime,
    workflow_engine=getattr(router, "workflow_engine", None),
    run_router=router,
    context_manager=context_manager,
    production_bundle=_production_bundle,
)
saas_runtime = build_saas_product_runtime(finops=getattr(router, "finops", None), production_bundle=_production_bundle)
accounts_runtime = build_accounts_runtime(
    saas_store=saas_runtime.store,
    saas_billing=saas_runtime.service.billing,
)
configure_accounts_auth(accounts_runtime.service)
install_dual_auth()
ba_api_runtime = build_business_assistant_api_runtime()
tg_interface_runtime = None
if telegram_interface_enabled():
    tg_interface_runtime = build_telegram_interface_runtime(
        ba_api=ba_api_runtime.service,
        upload_dir=ba_api_runtime.upload_dir,
    )
voice_interface_runtime = None
if voice_interface_enabled():
    voice_interface_runtime = build_voice_interface_runtime(ba_api=ba_api_runtime.service)
ops_admin_runtime = build_operations_admin_runtime(
    side_effect_runtime=side_effect_runtime,
    router=router,
    saas_store=saas_runtime.store,
)
analytics_runtime = build_analytics_dashboard_runtime(
    integration_activation=getattr(ba_api_runtime.service, "integration_activation", None),
)
scheduled_automation_runtime = build_scheduled_automation_runtime(
    workflow_runtime=getattr(side_effect_runtime, "workflow_runtime", None),
)
controlled_automation_runtime = build_controlled_automation_runtime(
    workflow_runtime=getattr(side_effect_runtime, "workflow_runtime", None),
    scheduled_automation=scheduled_automation_runtime.service,
)
scale_optimization_runtime = build_scale_optimization_runtime()
ba = ba_api_runtime.service.ba
for _attr, _svc in (
    ("analytics_dashboard", analytics_runtime.service),
    ("scheduled_automation", scheduled_automation_runtime.service),
    ("controlled_automation", controlled_automation_runtime.service),
):
    try:
        setattr(ba, _attr, _svc)
    except AttributeError:
        pass
from business_assistant_api.runtime import wire_panda_conversation_gateway

wire_panda_conversation_gateway(
    ba_service=ba,
    workflow_engine=getattr(router, "workflow_engine", None),
    run_router=getattr(router, "run", None),
    context_manager=context_manager,
)
_persistence = getattr(side_effect_runtime, "persistence", None)
_pf_connection = getattr(_persistence, "connection", None) if _persistence else None
_pf_ready = bool(_persistence and _persistence.ready)
try:
    production_foundation_runtime = initialize_production_foundation(
        side_effect_connection=_pf_connection,
        saas_store=saas_runtime.store,
        persistence_ready=_pf_ready,
        fail_closed=False,
    )
except Exception:
    from production_foundation.runtime import build_production_foundation_runtime

    production_foundation_runtime = build_production_foundation_runtime(
        side_effect_connection=_pf_connection,
        saas_store=saas_runtime.store,
        persistence_ready=_pf_ready,
    )
ops_admin_runtime.service.production_foundation = production_foundation_runtime.service
ops_admin_runtime.service.production_integrations = production_integration_runtime
try:
    assert_production_safe()
except RuntimeError:
    pass
try:
    from production_foundation.config import assert_production_startup_safe

    assert_production_startup_safe()
except Exception:
    pass


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


class ApprovalActionRequest(BaseModel):
    """Backward-compat only — identity is taken from RequestSecurityContext."""

    approver_id: str | None = None
    approver_role: str | None = None
    tenant_id: str | None = None
    expected_version: int | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Read-only repo probe if GITHUB_WRITE_PROBE_ON_STARTUP=true. No writes.

    Starts background TaskQueue worker for durable workflows when enabled.
    """
    await side_effect_runtime.start()
    try:
        yield
    finally:
        from config.runtime_health import DRAIN, begin_worker_drain

        DRAIN.begin_drain()
        wr = getattr(side_effect_runtime, "workflow_runtime", None)
        if wr is not None:
            try:
                await begin_worker_drain(wr, wait_seconds=0.5)
            except Exception:
                try:
                    await wr.stop_background()
                except Exception:
                    pass
        try:
            ui_chat_runtime.close()
        except Exception:
            pass
        try:
            ops_admin_runtime.close()
        except Exception:
            pass
        try:
            saas_runtime.close()
        except Exception:
            pass
        try:
            production_foundation_runtime.close()
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
app.add_middleware(PublicRateLimitMiddleware)

app.include_router(configure_ui_chat_router(ui_chat_runtime.service))
app.include_router(configure_operations_admin_router(ops_admin_runtime.service, ops_admin_runtime.policy))
app.include_router(configure_analytics_dashboard_router(analytics_runtime.service, analytics_runtime.policy))
app.include_router(configure_scheduled_automation_router(scheduled_automation_runtime.service, scheduled_automation_runtime.policy))
app.include_router(configure_controlled_automation_router(controlled_automation_runtime.service, controlled_automation_runtime.policy))
app.include_router(configure_scale_optimization_router(scale_optimization_runtime.service, scale_optimization_runtime.policy))
app.include_router(configure_saas_product_router(saas_runtime.service))
app.include_router(configure_accounts_router(accounts_runtime.service))
app.include_router(
    configure_business_assistant_api_router(ba_api_runtime.service, upload_dir=ba_api_runtime.upload_dir)
)
if tg_interface_runtime is not None:
    app.include_router(
        configure_telegram_interface_router(
            tg_interface_runtime.service,
            webhook_secret=tg_interface_runtime.webhook_secret,
        )
    )
if voice_interface_runtime is not None:
    app.include_router(configure_voice_interface_router(voice_interface_runtime.service))
_stripe_provider = _production_bundle.billing_provider if getattr(_production_bundle.billing_provider, "name", "") == "stripe" else None
app.include_router(
    configure_production_integration_router(
        b2b_service=getattr(getattr(side_effect_runtime, "b2b_commerce_runtime", None), "service", None),
        billing_service=saas_runtime.service.billing,
        stripe_provider=_stripe_provider,
        telegram_secret=str(os.environ.get("TELEGRAM_WEBHOOK_SECRET") or ""),
    )
)
app.mount("/static", StaticFiles(directory="static"), name="static")

_hitl_http = HitlHttpAuthorizer(resource_authorizer=get_resource_authorizer())

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
        # Tenant-scoped lookup — cross-tenant id resolves as not found.
        sm = router.workflow_engine.state_manager
        if hasattr(sm, "get_for_tenant"):
            state = sm.get_for_tenant(workflow_id, ctx.tenant_id)
        else:
            state = sm.get(workflow_id)
        authorizer.authorize_workflow_access(ctx, state, permission=permission)
    except (ResourceNotFoundError, WorkflowNotFoundError):
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
    """Liveness: process is up. Provider map is informational only (no provider HTTP)."""

    return HealthResponse(
        status="ok",
        providers=router.provider_status(),
    )


@app.get("/ready", response_model=ReadyResponse)
async def ready():
    """Readiness: role can perform its work (bounded dependency checks).

    ``capabilities`` exposes routing health/stats scope (process_local today).
    Lack of shared cross-worker routing health does not fail liveness or ordinary
    single-process readiness.
    """

    from config.runtime_health import evaluate_readiness

    health_tracker = getattr(router, "health_tracker", None)
    runtime_stats = getattr(router, "runtime_stats", None)
    snap = evaluate_readiness(
        side_effect_runtime=side_effect_runtime,
        runtime_config=_RUNTIME_CONFIG,
        health_tracker=health_tracker,
        runtime_stats=runtime_stats,
    )
    # Attach optional stores for dependency checks when wired on router.
    if getattr(router, "budget_guard", None) is not None:
        side_effect_runtime.budget_store = getattr(router.budget_guard, "store", None)
    if getattr(router, "provider_governor", None) is not None:
        side_effect_runtime.provider_governor = router.provider_governor
        snap = evaluate_readiness(
            side_effect_runtime=side_effect_runtime,
            runtime_config=_RUNTIME_CONFIG,
            health_tracker=health_tracker,
            runtime_stats=runtime_stats,
        )
    body = ReadyResponse(
        liveness=snap.liveness,
        readiness=snap.readiness,
        role=snap.role,
        draining=snap.draining,
        dependencies=[
            {"name": d.name, "status": d.status, "detail": d.detail}
            for d in snap.dependencies
        ],
        capabilities=dict(snap.capabilities or {}),
    )
    if snap.readiness == "not_ready":
        raise HTTPException(status_code=503, detail=body.model_dump())
    return body


@app.get("/metrics/runtime")
async def runtime_metrics():
    """Aggregated ops metrics (no prompts; tenant ids anonymized)."""

    from observability.runtime_metrics import collect_operational_metrics

    if getattr(router, "provider_governor", None) is not None:
        side_effect_runtime.provider_governor = router.provider_governor
    return collect_operational_metrics(
        side_effect_runtime=side_effect_runtime,
        provider_governor=getattr(router, "provider_governor", None),
        health_tracker=getattr(router, "health_tracker", None),
        runtime_stats=getattr(router, "runtime_stats", None),
    )


@app.post("/admin/drain", response_model=DrainResponse)
async def admin_drain(
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
    wait_seconds: float = 0.0,
):
    """Begin graceful drain: readiness fails; workers stop new claims."""

    from config.runtime_health import DRAIN, begin_worker_drain, evaluate_readiness

    get_resource_authorizer().require_permission(ctx, PERM_OPS_WRITE)
    get_audit_log().record(
        "admin.drain",
        actor_ref=ctx.actor_ref(),
        tenant_ref=ctx.tenant_id,
        outcome="ok",
    )
    DRAIN.begin_drain()
    wr = getattr(side_effect_runtime, "workflow_runtime", None)
    if wr is not None and wait_seconds > 0:
        await begin_worker_drain(wr, wait_seconds=min(float(wait_seconds), 30.0))
    elif wr is not None and hasattr(wr, "stop_new_claims"):
        wr.stop_new_claims()
    snap = evaluate_readiness(
        side_effect_runtime=side_effect_runtime,
        runtime_config=_RUNTIME_CONFIG,
    )
    return DrainResponse(draining=True, readiness=snap.readiness)


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
            request_id=ctx.request_id,
            user_id=ctx.user_id,
            actor_ref=ctx.actor_ref(),
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

    except MissingTenantError:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "missing_tenant",
                "message": "tenant_id is required for new execution.",
            },
        )

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
                request_id=ctx.request_id,
                user_id=ctx.user_id,
                actor_ref=ctx.actor_ref(),
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
            request_id=ctx.request_id,
            user_id=ctx.user_id,
            actor_ref=ctx.actor_ref(),
        )
        status = workflow_runtime.get_status(created["workflow_id"])
        status["queue_task_id"] = created.get("queue_task_id")
        return WorkflowStatusResponse(**{
            k: status.get(k)
            for k in WorkflowStatusResponse.model_fields
            if k in status or k == "queue_task_id"
        })
    except MissingTenantError:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "missing_tenant",
                "message": "tenant_id is required for new durable workflow.",
            },
        )
    except Exception as exc:
        from workflow.admission import AdmissionRejectedError

        if isinstance(exc, AdmissionRejectedError):
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "admission_rejected",
                    "reason": exc.reason,
                    "decision": exc.decision,
                },
            ) from exc
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


@app.post("/api/workflows/{workflow_id}/approvals/{approval_id}/approve")
async def approve_workflow_hitl(
    workflow_id: str,
    approval_id: str,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
    body: ApprovalActionRequest | None = None,
):
    hitl = getattr(side_effect_runtime, "hitl_service", None)
    if hitl is None or workflow_runtime is None:
        raise HTTPException(status_code=503, detail={"error": "hitl_unavailable"})
    try:
        state = router.workflow_engine.state_manager.get(workflow_id)
        payload = HitlActionPayload(
            approver_id=body.approver_id if body else None,
            approver_role=body.approver_role if body else None,
            tenant_id=body.tenant_id if body else None,
            expected_version=body.expected_version if body else None,
        )
        record = _hitl_http.approve(
            ctx,
            approval_id=approval_id,
            workflow_id=workflow_id,
            hitl=hitl,
            workflow_state=state,
            payload=payload,
        )
        get_audit_log().record(
            "hitl.approved",
            actor_ref=ctx.actor_ref(),
            tenant_ref=ctx.tenant_id,
            resource_ref=approval_id,
            outcome="ok",
        )
        return {
            "approval_id": record.approval_id,
            "workflow_id": record.workflow_id,
            "status": record.status,
        }
    except ResourceNotFoundError:
        raise HTTPException(status_code=404, detail={"error": "approval_not_found"})
    except UnauthorizedError as exc:
        raise HTTPException(status_code=403, detail={"error": getattr(exc, "error_code", "unauthorized")})


@app.post("/api/workflows/{workflow_id}/approvals/{approval_id}/reject")
async def reject_workflow_hitl(
    workflow_id: str,
    approval_id: str,
    ctx: Annotated[RequestSecurityContext, Depends(get_security_context)],
    body: ApprovalActionRequest | None = None,
):
    hitl = getattr(side_effect_runtime, "hitl_service", None)
    if hitl is None or workflow_runtime is None:
        raise HTTPException(status_code=503, detail={"error": "hitl_unavailable"})
    try:
        state = router.workflow_engine.state_manager.get(workflow_id)
        payload = HitlActionPayload(
            approver_id=body.approver_id if body else None,
            approver_role=body.approver_role if body else None,
            tenant_id=body.tenant_id if body else None,
            expected_version=body.expected_version if body else None,
        )
        record = _hitl_http.reject(
            ctx,
            approval_id=approval_id,
            workflow_id=workflow_id,
            hitl=hitl,
            workflow_state=state,
            payload=payload,
        )
        get_audit_log().record(
            "hitl.rejected",
            actor_ref=ctx.actor_ref(),
            tenant_ref=ctx.tenant_id,
            resource_ref=approval_id,
            outcome="ok",
        )
        return {
            "approval_id": record.approval_id,
            "workflow_id": record.workflow_id,
            "status": record.status,
        }
    except ResourceNotFoundError:
        raise HTTPException(status_code=404, detail={"error": "approval_not_found"})
    except UnauthorizedError as exc:
        raise HTTPException(status_code=403, detail={"error": getattr(exc, "error_code", "unauthorized")})


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
    with open("static/panda/index.html", encoding="utf-8") as fh:
        return fh.read()


@app.get("/legacy-chat", response_class=HTMLResponse, include_in_schema=False)
async def legacy_chat_ui() -> str:
    with open("static/chat/index.html", encoding="utf-8") as fh:
        return fh.read()


@app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
async def admin_ui() -> str:
    with open("static/admin/index.html", encoding="utf-8") as fh:
        return fh.read()


@app.get("/owner", response_class=HTMLResponse, include_in_schema=False)
async def owner_ui() -> str:
    with open("static/owner/index.html", encoding="utf-8") as fh:
        return fh.read()


@app.get("/product", response_class=HTMLResponse, include_in_schema=False)
async def product_ui() -> str:
    with open("static/product/settings.html", encoding="utf-8") as fh:
        return fh.read()


@app.get("/analytics", response_class=HTMLResponse, include_in_schema=False)
async def analytics_ui() -> str:
    with open("static/analytics/index.html", encoding="utf-8") as fh:
        return fh.read()


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_ui() -> str:
    with open("static/accounts/login.html", encoding="utf-8") as fh:
        return fh.read()


@app.get("/terms", response_class=HTMLResponse, include_in_schema=False)
async def terms_ui() -> str:
    with open("static/accounts/terms.html", encoding="utf-8") as fh:
        return fh.read()


@app.get("/privacy", response_class=HTMLResponse, include_in_schema=False)
async def privacy_ui() -> str:
    with open("static/accounts/privacy.html", encoding="utf-8") as fh:
        return fh.read()


@app.get("/personal-data", response_class=HTMLResponse, include_in_schema=False)
async def personal_data_ui() -> str:
    with open("static/accounts/personal-data.html", encoding="utf-8") as fh:
        return fh.read()


@app.get("/ai-disclosure", response_class=HTMLResponse, include_in_schema=False)
async def ai_disclosure_ui() -> str:
    with open("static/accounts/ai-disclosure.html", encoding="utf-8") as fh:
        return fh.read()
