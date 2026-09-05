"""Deterministic multi-turn action continuation for conversational Panda.

Composes with existing follow-up resolution. Does not call models.
Does not replace Router, Pipeline, ToolGateway, or conversation history.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping

from agents.routing_requirements import FRESHNESS_CURRENT, derive_task_requirements
from autonomy.capabilities import CAP_IMAGE_EDIT, CAP_IMAGE_GENERATE
from business_assistant.follow_up import (
    KIND_NEW_TOPIC,
    KIND_REFERENT,
    KIND_TRANSFORM,
    FollowUpResolution,
)
from business_assistant.intent import requires_business_integration
from security.tenant import require_tenant_id

TOOL_EXCEL_INSPECT = "excel.inspect"
TOOL_IMAGE_EDIT = "image.edit"
TOOL_IMAGE_GENERATE = "image.generate"


# --- Public decision / lifecycle labels (internal only) -------------------

CONTINUE_ACTIVE_TASK = "CONTINUE_ACTIVE_TASK"
NEW_TASK = "NEW_TASK"
AMBIGUOUS = "AMBIGUOUS"

READY_TO_EXECUTE = "READY_TO_EXECUTE"
NEEDS_REQUIRED_INPUT = "NEEDS_REQUIRED_INPUT"
NEEDS_APPROVAL = "NEEDS_APPROVAL"
NOT_EXECUTABLE = "NOT_EXECUTABLE"
CONVERSATIONAL_ONLY = "CONVERSATIONAL_ONLY"

ANSWER_TEXT = "ANSWER_TEXT"
CALL_TOOL = "CALL_TOOL"
ASK_CLARIFICATION = "ASK_CLARIFICATION"
REQUEST_APPROVAL = "REQUEST_APPROVAL"
FAIL_UNAVAILABLE = "FAIL_UNAVAILABLE"

STATUS_DRAFT = "DRAFT"
STATUS_WAITING_FOR_INPUT = "WAITING_FOR_INPUT"
STATUS_READY = "READY"
STATUS_EXECUTING = "EXECUTING"
STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED_RETRYABLE = "FAILED_RETRYABLE"
STATUS_CANCELLED = "CANCELLED"
STATUS_SUPERSEDED = "SUPERSEDED"

FAMILY_IMAGE_GENERATE = "image_generate"
FAMILY_IMAGE_EDIT = "image_edit"
FAMILY_SEARCH = "search"
FAMILY_EXCEL = "excel"
FAMILY_DOCUMENT = "document"
FAMILY_WRITE = "write_governed"

RISK_GENERATE = "generate"
RISK_READ = "read"
RISK_WRITE = "write_governed"

CAPABILITY_AVAILABLE_AND_AUTHORIZED = "CAPABILITY_AVAILABLE_AND_AUTHORIZED"
CAPABILITY_AVAILABLE_REQUIRES_APPROVAL = "CAPABILITY_AVAILABLE_REQUIRES_APPROVAL"
CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
CAPABILITY_MISCONFIGURED = "CAPABILITY_MISCONFIGURED"

PARAM_SCENE = "scene_description"
PARAM_QUANTITY = "variant_count"
PARAM_ASPECT = "aspect_ratio"
PARAM_STYLE = "style"
PARAM_FILE = "file_ref"


@dataclass(frozen=True)
class CapabilityContract:
    family: str
    tool_id: str
    operation: str
    required: tuple[str, ...]
    optional: tuple[str, ...]
    defaults: Mapping[str, Any]
    risk: str
    required_capabilities: tuple[str, ...]
    artifact_type: str = ""


IMAGE_GENERATE_CONTRACT = CapabilityContract(
    family=FAMILY_IMAGE_GENERATE,
    tool_id=TOOL_IMAGE_GENERATE,
    operation="generate",
    required=(PARAM_SCENE,),
    optional=(PARAM_ASPECT, PARAM_QUANTITY, PARAM_STYLE),
    defaults={PARAM_ASPECT: "1:1", PARAM_QUANTITY: 1},
    risk=RISK_GENERATE,
    required_capabilities=(CAP_IMAGE_GENERATE,),
    artifact_type="image",
)

IMAGE_EDIT_CONTRACT = CapabilityContract(
    family=FAMILY_IMAGE_EDIT,
    tool_id=TOOL_IMAGE_EDIT,
    operation="edit",
    required=("source_version_id", "instruction"),
    optional=(),
    defaults={},
    risk=RISK_GENERATE,
    required_capabilities=(CAP_IMAGE_EDIT,),
    artifact_type="image",
)

EXCEL_CONTRACT = CapabilityContract(
    family=FAMILY_EXCEL,
    tool_id=TOOL_EXCEL_INSPECT,
    operation="inspect",
    required=(PARAM_FILE,),
    optional=(),
    defaults={},
    risk=RISK_READ,
    required_capabilities=(),
    artifact_type="workbook",
)

CONTRACTS: dict[str, CapabilityContract] = {
    FAMILY_IMAGE_GENERATE: IMAGE_GENERATE_CONTRACT,
    FAMILY_IMAGE_EDIT: IMAGE_EDIT_CONTRACT,
    FAMILY_EXCEL: EXCEL_CONTRACT,
}


@dataclass
class ActiveTask:
    task_id: str
    tenant_id: str
    owner_id: str
    conversation_id: str
    family: str
    tool_id: str
    operation: str
    goal: str
    parameters: dict[str, Any] = field(default_factory=dict)
    missing_required: tuple[str, ...] = ()
    quantity: int | None = None
    artifact_type: str = ""
    status: str = STATUS_DRAFT
    execute_requested: bool = False
    execution_count: int = 0
    last_idempotency_key: str = ""
    last_artifact_ids: tuple[str, ...] = ()
    awaiting_quantity: bool = False
    risk: str = RISK_GENERATE

    def snapshot(self) -> "ActiveTask":
        return ActiveTask(
            task_id=self.task_id,
            tenant_id=self.tenant_id,
            owner_id=self.owner_id,
            conversation_id=self.conversation_id,
            family=self.family,
            tool_id=self.tool_id,
            operation=self.operation,
            goal=self.goal,
            parameters=dict(self.parameters),
            missing_required=tuple(self.missing_required),
            quantity=self.quantity,
            artifact_type=self.artifact_type,
            status=self.status,
            execute_requested=self.execute_requested,
            execution_count=self.execution_count,
            last_idempotency_key=self.last_idempotency_key,
            last_artifact_ids=tuple(self.last_artifact_ids),
            awaiting_quantity=self.awaiting_quantity,
            risk=self.risk,
        )


@dataclass(frozen=True)
class ActionDecision:
    decision: str
    readiness: str
    continuation: str
    task: ActiveTask | None
    arguments: dict[str, Any] = field(default_factory=dict)
    user_message: str = ""
    tool_id: str = ""
    operation: str = ""
    extra_llm: bool = False
    capability_status: str = CAPABILITY_UNAVAILABLE
    idempotency_key: str = ""


class ActiveTaskStore:
    """In-process active-task frame keyed by existing conversation identity."""

    def __init__(self):
        self._tasks: dict[tuple[str, str, str], ActiveTask] = {}

    def _key(self, tenant_id: str, owner_id: str, conversation_id: str) -> tuple[str, str, str]:
        return (require_tenant_id(tenant_id), str(owner_id or ""), str(conversation_id or ""))

    def get(self, *, tenant_id: str, owner_id: str, conversation_id: str) -> ActiveTask | None:
        if not conversation_id:
            return None
        task = self._tasks.get(self._key(tenant_id, owner_id, conversation_id))
        return task.snapshot() if task is not None else None

    def put(self, task: ActiveTask) -> None:
        if not task.conversation_id:
            return
        self._tasks[self._key(task.tenant_id, task.owner_id, task.conversation_id)] = task.snapshot()

    def clear(self, *, tenant_id: str, owner_id: str, conversation_id: str) -> None:
        if not conversation_id:
            return
        self._tasks.pop(self._key(tenant_id, owner_id, conversation_id), None)


def _norm(text: str) -> str:
    raw = (text or "").strip().casefold().replace("ё", "е")
    return re.sub(r"\s+", " ", raw)


def _has_stem(text: str, stems: tuple[str, ...]) -> bool:
    blob = _norm(text)
    if not blob:
        return False
    return any(_norm(stem) in blob for stem in stems if stem)


def _token_count(text: str) -> int:
    return len(_norm(text).split())


_IMAGE_ARTIFACT_STEMS = (
    "image",
    "picture",
    "photo",
    "illustrat",
    "artwork",
    "avatar",
    "logo",
    "картин",
    "изобр",
    "фото",
    "логотип",
    "рисун",
    "иллюстрац",
)
_IMAGE_VERB_STEMS = (
    "generat",
    "draw",
    "paint",
    "create",
    "сгенер",
    "сгенен",
    "нарис",
    "создай",
    "создать",
)
_MAKE_STEMS = ("make", "сделай", "сделать", "сделаем")
_EXECUTE_STEMS = _IMAGE_VERB_STEMS + ("сделай", "сделать", "make it", "do it")
_CANCEL_STEMS = ("cancel", "отмен", "стоп", "stop that")
_SEVERAL_STEMS = ("several", "multiple", "a few", "нескольк", "пару", "несколько")
_LOGO_STEMS = ("logo", "логотип")
_SQUARE_STEMS = ("square", "квадрат")
_REALISM_STEMS = ("realism", "realistic", "реализм", "реалистич")
_NIGHT_STEMS = ("night", "ноч")
_DAY_STEMS = ("daytime", "day", "днем", "днём", "день")
_FOREST_STEMS = ("forest", "лес")
_WOLF_STEMS = ("wolf", "волк")
_CORRECTION_STEMS = ("нет,", "no,", "не то", "instead")
_YES_STEMS = ("да", "yes", "ок", "ok", "угу")
_EXCEL_STEMS = ("excel", "xlsx", "csv", "spreadsheet", "таблиц")
_DOC_STEMS = ("document", "pdf", "docx", "документ")
_SEARCH_STEMS = ("search", "найди", "find ", "google")
_WEATHER_STEMS = ("weather", "погод")
_QUESTION_NEW_STEMS = (
    "что такое",
    "what is",
    "расскажи",
    "tell me",
    "how does",
    "как работает",
    "для ип",
    "налог",
)
_WRITE_STEMS = (
    "удали заказ",
    "измени цен",
    "опубликуй",
    "отправь письмо",
    "измени остат",
    "delete order",
    "change price",
    "publish product",
    "send email",
)


def _is_image_artifact_request(text: str) -> bool:
    return _has_stem(text, _IMAGE_ARTIFACT_STEMS)


def _is_image_execute_verb(text: str) -> bool:
    return _has_stem(text, _IMAGE_VERB_STEMS) or (
        _has_stem(text, _MAKE_STEMS) and _is_image_artifact_request(text)
    )


def _is_cancel(text: str) -> bool:
    blob = _norm(text)
    if blob in {"отмена", "cancel", "стоп", "stop"}:
        return True
    return _token_count(text) <= 3 and _has_stem(text, _CANCEL_STEMS)


def _is_yes(text: str) -> bool:
    blob = _norm(text).strip(" !.?,")
    return blob in {"да", "yes", "ок", "ok", "угу", "ага"}


def _is_unrelated_new_task(text: str) -> bool:
    if requires_business_integration(text):
        return True
    if _has_stem(text, _WEATHER_STEMS):
        return True
    if _has_stem(text, _QUESTION_NEW_STEMS) and not _is_image_artifact_request(text):
        return True
    req = derive_task_requirements(category="general", text=text)
    if str(getattr(req, "freshness", "") or "") == FRESHNESS_CURRENT:
        return True
    return False


def _extract_quantity(text: str) -> int | None:
    blob = _norm(text)
    if _has_stem(blob, _SEVERAL_STEMS):
        return None
    match = re.search(r"\b(\d{1,2})\b", blob)
    if match:
        value = int(match.group(1))
        if 1 <= value <= 16:
            return value
    words = {
        "one": 1,
        "two": 2,
        "three": 3,
        "один": 1,
        "одна": 1,
        "два": 2,
        "две": 2,
        "три": 3,
    }
    for token in blob.split():
        if token in words:
            return words[token]
    return None


def _is_quantity_only(text: str) -> bool:
    blob = _norm(text).strip(" !.?,")
    if re.fullmatch(r"\d{1,2}", blob):
        return True
    if blob in {"один", "одна", "два", "две", "три", "one", "two", "three"}:
        return True
    return bool(re.fullmatch(r"(сделай|make)\s+\d{1,2}", blob))


def _strip_scene_wrappers(text: str) -> str:
    blob = (text or "").strip()
    blob = re.sub(
        r"^\s*(please|пожалуйста)\s+",
        "",
        blob,
        flags=re.I,
    )
    blob = re.sub(
        r"\b(сделай(те)?|сделать|create|make|draw|generate|сгенерируй|сгененрировать|"
        r"сгенерировать|нарисуй|создай|создать)\b",
        " ",
        blob,
        flags=re.I,
    )
    blob = re.sub(
        r"\b(мне|для\s+меня|please|a|an|the|with|с|картинк\w*|изображени\w*|"
        r"изоброжени\w*|фото\w*|image\w*|picture\w*|photo\w*|logo|логотип)\b",
        " ",
        blob,
        flags=re.I,
    )
    blob = re.sub(r"\s+", " ", blob).strip(" ,.-")
    return blob


def _merge_scene(existing: str, addition: str, *, replace_time: bool = False) -> str:
    base = (existing or "").strip()
    extra = (addition or "").strip()
    if not extra:
        return base
    extra_norm = _norm(extra)
    if replace_time:
        parts = [
            tok
            for tok in re.split(r"\s+", base)
            if tok and not _has_stem(tok, _NIGHT_STEMS + _DAY_STEMS)
        ]
        base = " ".join(parts)
    if extra_norm and extra_norm in _norm(base):
        return base
    return f"{base} {extra}".strip()


def _apply_style(parameters: dict[str, Any], text: str) -> None:
    if _has_stem(text, _LOGO_STEMS):
        parameters[PARAM_STYLE] = "logo"
    if _has_stem(text, _REALISM_STEMS):
        parameters[PARAM_STYLE] = "realistic"
    if _has_stem(text, _SQUARE_STEMS):
        parameters[PARAM_ASPECT] = "1:1"


def _apply_time_setting(parameters: dict[str, Any], text: str, *, correction: bool) -> None:
    scene = str(parameters.get(PARAM_SCENE) or "")
    if _has_stem(text, _NIGHT_STEMS):
        parameters[PARAM_SCENE] = _merge_scene(scene, "night", replace_time=correction or _has_stem(scene, _DAY_STEMS))
        scene = str(parameters.get(PARAM_SCENE) or "")
    if _has_stem(text, _DAY_STEMS) and not _has_stem(text, _NIGHT_STEMS):
        parameters[PARAM_SCENE] = _merge_scene(scene, "day", replace_time=True)
        scene = str(parameters.get(PARAM_SCENE) or "")
    if _has_stem(text, _FOREST_STEMS):
        parameters[PARAM_SCENE] = _merge_scene(scene, "forest")
        scene = str(parameters.get(PARAM_SCENE) or "")
    if _has_stem(text, _WOLF_STEMS):
        parameters[PARAM_SCENE] = _merge_scene(scene, "wolf")


def detect_family(text: str, active: ActiveTask | None) -> str | None:
    raw = text or ""
    if _has_stem(raw, _WRITE_STEMS):
        return FAMILY_WRITE
    if _has_stem(raw, _EXCEL_STEMS) and (
        _has_stem(raw, ("анализ", "analyze", "inspect", "проанализ")) or _has_stem(raw, _MAKE_STEMS)
    ):
        return FAMILY_EXCEL
    if _is_image_artifact_request(raw) or (
        _is_image_execute_verb(raw) and (active is None or active.family == FAMILY_IMAGE_GENERATE)
    ):
        return FAMILY_IMAGE_GENERATE
    if active is not None and active.family == FAMILY_IMAGE_GENERATE:
        if _is_image_execute_verb(raw) or _is_quantity_only(raw) or _has_stem(raw, _SEVERAL_STEMS):
            return FAMILY_IMAGE_GENERATE
        if _token_count(raw) <= 8 and not _is_unrelated_new_task(raw):
            return FAMILY_IMAGE_GENERATE
    if _has_stem(raw, _SEARCH_STEMS) and _is_unrelated_new_task(raw):
        return FAMILY_SEARCH
    if _has_stem(raw, _DOC_STEMS) and _has_stem(raw, _MAKE_STEMS + _IMAGE_VERB_STEMS):
        return FAMILY_DOCUMENT
    return None


def continuation_decision(
    text: str,
    *,
    active: ActiveTask | None,
    follow_up: FollowUpResolution | None = None,
) -> str:
    if active is None:
        return NEW_TASK
    if follow_up is not None and follow_up.kind in {KIND_TRANSFORM, KIND_REFERENT}:
        if not detect_family(text, active):
            return NEW_TASK
    if _is_unrelated_new_task(text) and not _is_quantity_only(text):
        return NEW_TASK
    family = detect_family(text, active)
    if family == FAMILY_WRITE:
        return NEW_TASK
    if family and family != active.family and family not in {FAMILY_IMAGE_EDIT, FAMILY_IMAGE_GENERATE}:
        if family == FAMILY_IMAGE_GENERATE and active.family == FAMILY_IMAGE_EDIT:
            return CONTINUE_ACTIVE_TASK
        if family == FAMILY_IMAGE_EDIT and active.family == FAMILY_IMAGE_GENERATE:
            return CONTINUE_ACTIVE_TASK
        return NEW_TASK
    if family == active.family or family is None:
        if _token_count(text) <= 10 or family == active.family:
            return CONTINUE_ACTIVE_TASK
    if _token_count(text) <= 2:
        return AMBIGUOUS
    return NEW_TASK


def _contract_for(family: str | None) -> CapabilityContract | None:
    if not family:
        return None
    return CONTRACTS.get(family)


def compute_readiness(task: ActiveTask) -> str:
    if task.status == STATUS_CANCELLED:
        return NOT_EXECUTABLE
    if task.risk == RISK_WRITE:
        return NEEDS_APPROVAL
    contract = _contract_for(task.family)
    if contract is None:
        return CONVERSATIONAL_ONLY
    missing: list[str] = []
    params = dict(task.parameters)
    for key in contract.required:
        value = params.get(key)
        if value in (None, "", [], ()):
            missing.append(key)
    if task.awaiting_quantity and not task.quantity:
        missing.append(PARAM_QUANTITY)
    task.missing_required = tuple(missing)
    if missing:
        return NEEDS_REQUIRED_INPUT
    return READY_TO_EXECUTE


def _apply_defaults(task: ActiveTask) -> dict[str, Any]:
    contract = _contract_for(task.family)
    args = dict(task.parameters)
    if contract is None:
        return args
    for key, value in dict(contract.defaults).items():
        args.setdefault(key, value)
    if task.quantity:
        args[PARAM_QUANTITY] = int(task.quantity)
    else:
        args.setdefault(PARAM_QUANTITY, int(contract.defaults.get(PARAM_QUANTITY, 1) or 1))
    scene = str(args.get(PARAM_SCENE) or "").strip()
    style = str(args.get(PARAM_STYLE) or "").strip()
    if style and style not in scene.casefold():
        args[PARAM_SCENE] = f"{scene} {style}".strip()
    args["prompt"] = str(args.get(PARAM_SCENE) or "")
    return args


def _clarification_for(task: ActiveTask) -> str:
    missing = task.missing_required
    if PARAM_QUANTITY in missing:
        return "Сколько вариантов?"
    if PARAM_SCENE in missing or "instruction" in missing:
        return "Что нужно сделать?"
    if PARAM_FILE in missing:
        return "Приложите файл."
    if missing:
        return "Нужен ещё один обязательный параметр."
    return "Уточните, пожалуйста."


def _bind_image_params(task: ActiveTask, text: str, *, correction: bool) -> None:
    _apply_style(task.parameters, text)
    qty = _extract_quantity(text)
    if _has_stem(text, _SEVERAL_STEMS) and qty is None:
        task.awaiting_quantity = True
        task.quantity = None
        task.parameters.pop(PARAM_QUANTITY, None)
    elif qty is not None:
        task.quantity = qty
        task.parameters[PARAM_QUANTITY] = qty
        task.awaiting_quantity = False
    _apply_time_setting(task.parameters, text, correction=correction)
    remainder = _strip_scene_wrappers(text)
    skip_remainder = (
        _is_quantity_only(text)
        or _has_stem(text, _SEVERAL_STEMS)
        or _has_stem(text, _CORRECTION_STEMS)
    )
    if remainder and not skip_remainder:
        if _has_stem(remainder, _LOGO_STEMS) and _token_count(remainder) <= 2:
            pass
        else:
            task.parameters[PARAM_SCENE] = _merge_scene(
                str(task.parameters.get(PARAM_SCENE) or ""),
                remainder,
                replace_time=correction,
            )
    if _is_image_execute_verb(text) or _has_stem(text, _MAKE_STEMS):
        task.execute_requested = True
    task.goal = str(task.parameters.get(PARAM_SCENE) or task.goal)


def _new_image_task(*, tenant_id: str, owner_id: str, conversation_id: str, text: str) -> ActiveTask:
    task = ActiveTask(
        task_id=str(uuid.uuid4()),
        tenant_id=require_tenant_id(tenant_id),
        owner_id=str(owner_id or ""),
        conversation_id=str(conversation_id or ""),
        family=FAMILY_IMAGE_GENERATE,
        tool_id=IMAGE_GENERATE_CONTRACT.tool_id,
        operation=IMAGE_GENERATE_CONTRACT.operation,
        goal=_strip_scene_wrappers(text) or text,
        artifact_type="image",
        status=STATUS_DRAFT,
        risk=RISK_GENERATE,
        execute_requested=_is_image_execute_verb(text) or _has_stem(text, _MAKE_STEMS),
    )
    _bind_image_params(task, text, correction=False)
    return task


def user_unavailable_message(family: str) -> str:
    if family == FAMILY_IMAGE_GENERATE:
        return "Сейчас не могу создать изображение — возможность генерации недоступна."
    if family == FAMILY_EXCEL:
        return "Сейчас не могу обработать таблицу — возможность недоступна."
    if family == FAMILY_DOCUMENT:
        return "Сейчас не могу создать документ — возможность недоступна."
    return "Эта возможность сейчас недоступна."


def _user_tool_error() -> str:
    return "Не получилось выполнить действие. Можно повторить запрос."


def _user_success_image() -> str:
    return "Готово."


def inspect_capability(gateway, tool_id: str) -> str:
    if gateway is None:
        return CAPABILITY_UNAVAILABLE
    try:
        descriptor = gateway.get_tool(tool_id)
    except Exception:
        return CAPABILITY_UNAVAILABLE
    if descriptor is None:
        return CAPABILITY_UNAVAILABLE
    if not bool(getattr(descriptor, "enabled", False)):
        return CAPABILITY_UNAVAILABLE
    return CAPABILITY_AVAILABLE_AND_AUTHORIZED


def resolve_action_turn(
    text: str,
    *,
    tenant_id: str,
    owner_id: str,
    conversation_id: str,
    store: ActiveTaskStore,
    follow_up: FollowUpResolution | None = None,
    gateway=None,
    request_id: str = "",
) -> ActionDecision:
    """Pure-ish turn resolver. At most one extra LLM call: never (extra_llm=False)."""
    current = (text or "").strip()
    tenant = require_tenant_id(tenant_id)
    owner = str(owner_id or "")
    conv = str(conversation_id or "")
    active = store.get(tenant_id=tenant, owner_id=owner, conversation_id=conv)

    if follow_up is not None and follow_up.kind in {KIND_TRANSFORM, KIND_REFERENT}:
        if not (active and detect_family(current, active) == active.family and _is_image_artifact_request(current)):
            return ActionDecision(
                decision=ANSWER_TEXT,
                readiness=CONVERSATIONAL_ONLY,
                continuation=NEW_TASK if follow_up.kind == KIND_TRANSFORM else CONTINUE_ACTIVE_TASK,
                task=active,
                extra_llm=False,
            )

    if active is not None and _is_yes(current) and active.risk == RISK_WRITE:
        return ActionDecision(
            decision=REQUEST_APPROVAL,
            readiness=NEEDS_APPROVAL,
            continuation=CONTINUE_ACTIVE_TASK,
            task=active,
            user_message="Это действие требует отдельного подтверждения в запросе на одобрение.",
            extra_llm=False,
            capability_status=CAPABILITY_AVAILABLE_REQUIRES_APPROVAL,
        )

    if active is not None and _is_cancel(current):
        active.status = STATUS_CANCELLED
        store.put(active)
        return ActionDecision(
            decision=ANSWER_TEXT,
            readiness=NOT_EXECUTABLE,
            continuation=CONTINUE_ACTIVE_TASK,
            task=active,
            user_message="Отменил текущую задачу.",
            extra_llm=False,
        )

    mode = continuation_decision(current, active=active, follow_up=follow_up)
    family = detect_family(current, active if mode != NEW_TASK else None)

    if family == FAMILY_WRITE:
        if active is not None:
            active.status = STATUS_SUPERSEDED
            store.put(active)
        return ActionDecision(
            decision=ANSWER_TEXT,
            readiness=NEEDS_APPROVAL,
            continuation=NEW_TASK,
            task=None,
            extra_llm=False,
            capability_status=CAPABILITY_AVAILABLE_REQUIRES_APPROVAL,
        )

    if family == FAMILY_SEARCH or (
        mode == NEW_TASK and _is_unrelated_new_task(current) and family not in {FAMILY_IMAGE_GENERATE, FAMILY_EXCEL}
    ):
        if active is not None:
            active.status = STATUS_SUPERSEDED
            store.put(active)
        return ActionDecision(
            decision=ANSWER_TEXT,
            readiness=CONVERSATIONAL_ONLY,
            continuation=NEW_TASK,
            task=None,
            extra_llm=False,
        )

    if mode == NEW_TASK and family is None:
        if active is not None and _token_count(current) <= 2 and not _is_unrelated_new_task(current):
            mode = AMBIGUOUS
        else:
            if active is not None:
                active.status = STATUS_SUPERSEDED
                store.put(active)
            return ActionDecision(
                decision=ANSWER_TEXT,
                readiness=CONVERSATIONAL_ONLY,
                continuation=NEW_TASK,
                task=None,
                extra_llm=False,
            )

    if mode == AMBIGUOUS and active is not None and active.family == FAMILY_IMAGE_GENERATE:
        if _is_yes(current) and active.risk == RISK_WRITE:
            return ActionDecision(
                decision=REQUEST_APPROVAL,
                readiness=NEEDS_APPROVAL,
                continuation=CONTINUE_ACTIVE_TASK,
                task=active,
                user_message="Это действие требует отдельного подтверждения в запросе на одобрение.",
                extra_llm=False,
            )
        if _is_yes(current) and compute_readiness(active) == READY_TO_EXECUTE:
            active.execute_requested = True
            mode = CONTINUE_ACTIVE_TASK
        elif not _is_quantity_only(current) and not _has_stem(current, _IMAGE_ARTIFACT_STEMS + _IMAGE_VERB_STEMS):
            return ActionDecision(
                decision=ANSWER_TEXT,
                readiness=CONVERSATIONAL_ONLY,
                continuation=AMBIGUOUS,
                task=active,
                extra_llm=False,
            )

    if family == FAMILY_IMAGE_GENERATE or (
        active is not None and active.family == FAMILY_IMAGE_GENERATE and mode == CONTINUE_ACTIVE_TASK
    ):
        correction = _has_stem(current, _CORRECTION_STEMS)
        if mode == NEW_TASK or active is None or active.family != FAMILY_IMAGE_GENERATE:
            if active is not None:
                active.status = STATUS_SUPERSEDED
                store.put(active)
            task = _new_image_task(
                tenant_id=tenant, owner_id=owner, conversation_id=conv, text=current
            )
        else:
            task = active
            if task.status in {STATUS_COMPLETED, STATUS_FAILED_RETRYABLE}:
                task.status = STATUS_DRAFT
            _bind_image_params(task, current, correction=correction)
            if _is_image_execute_verb(current) or _has_stem(current, ("сгенер", "сгенен")):
                task.execute_requested = True

        readiness = compute_readiness(task)
        cap_status = inspect_capability(gateway, task.tool_id)
        args = _apply_defaults(task)

        if readiness == NEEDS_REQUIRED_INPUT:
            task.status = STATUS_WAITING_FOR_INPUT
            store.put(task)
            return ActionDecision(
                decision=ASK_CLARIFICATION,
                readiness=readiness,
                continuation=CONTINUE_ACTIVE_TASK if active is not None else NEW_TASK,
                task=task,
                arguments=args,
                user_message=_clarification_for(task),
                tool_id=task.tool_id,
                operation=task.operation,
                extra_llm=False,
                capability_status=cap_status,
            )

        is_new = mode == NEW_TASK or active is None or active.family != FAMILY_IMAGE_GENERATE
        should_execute = bool(task.execute_requested)
        if is_new and _is_image_artifact_request(current) and readiness == READY_TO_EXECUTE:
            should_execute = True
        if _is_quantity_only(current) and task.execute_requested and readiness == READY_TO_EXECUTE:
            should_execute = True

        if not should_execute:
            task.status = STATUS_READY if readiness == READY_TO_EXECUTE else STATUS_DRAFT
            store.put(task)
            return ActionDecision(
                decision=ANSWER_TEXT,
                readiness=readiness,
                continuation=CONTINUE_ACTIVE_TASK,
                task=task,
                arguments=args,
                extra_llm=False,
                capability_status=cap_status,
            )

        if cap_status != CAPABILITY_AVAILABLE_AND_AUTHORIZED:
            task.status = STATUS_FAILED_RETRYABLE
            store.put(task)
            return ActionDecision(
                decision=FAIL_UNAVAILABLE,
                readiness=NOT_EXECUTABLE,
                continuation=CONTINUE_ACTIVE_TASK,
                task=task,
                arguments=args,
                user_message=user_unavailable_message(FAMILY_IMAGE_GENERATE),
                tool_id=task.tool_id,
                operation=task.operation,
                extra_llm=False,
                capability_status=cap_status,
            )

        idem = _idempotency_key(request_id, task.tool_id, args)
        task.status = STATUS_READY
        store.put(task)
        return ActionDecision(
            decision=CALL_TOOL,
            readiness=READY_TO_EXECUTE,
            continuation=CONTINUE_ACTIVE_TASK if active is not None else NEW_TASK,
            task=task,
            arguments=args,
            tool_id=task.tool_id,
            operation=task.operation,
            extra_llm=False,
            capability_status=cap_status,
            idempotency_key=idem,
        )

    if family == FAMILY_EXCEL:
        task = ActiveTask(
            task_id=str(uuid.uuid4()),
            tenant_id=tenant,
            owner_id=owner,
            conversation_id=conv,
            family=FAMILY_EXCEL,
            tool_id=EXCEL_CONTRACT.tool_id,
            operation=EXCEL_CONTRACT.operation,
            goal=current,
            artifact_type="workbook",
            status=STATUS_WAITING_FOR_INPUT,
            missing_required=(PARAM_FILE,),
            risk=RISK_READ,
        )
        store.put(task)
        return ActionDecision(
            decision=ASK_CLARIFICATION,
            readiness=NEEDS_REQUIRED_INPUT,
            continuation=NEW_TASK,
            task=task,
            user_message="Приложите файл Excel.",
            extra_llm=False,
        )

    return ActionDecision(
        decision=ANSWER_TEXT,
        readiness=CONVERSATIONAL_ONLY,
        continuation=mode,
        task=active,
        extra_llm=False,
    )


def _idempotency_key(request_id: str, tool_id: str, arguments: Mapping[str, Any]) -> str:
    req = str(request_id or "").strip()
    if req:
        return f"{req}:{tool_id}"
    payload = hashlib.sha256(repr(sorted(dict(arguments).items())).encode("utf-8")).hexdigest()[:16]
    return f"{tool_id}:{payload}"


def mark_executed(store: ActiveTaskStore, task: ActiveTask, *, artifact_ids: tuple[str, ...] = (), failed: bool = False) -> None:
    if task.conversation_id:
        current = store.get(
            tenant_id=task.tenant_id, owner_id=task.owner_id, conversation_id=task.conversation_id
        ) or task
    else:
        current = task
    current.execution_count += 1
    current.execute_requested = False
    current.awaiting_quantity = False
    if failed:
        current.status = STATUS_FAILED_RETRYABLE
    else:
        current.status = STATUS_COMPLETED
        current.last_artifact_ids = tuple(artifact_ids)
    store.put(current)


def format_tool_user_text(*, family: str, data: Mapping[str, Any] | None, success: bool) -> str:
    if not success:
        return _user_tool_error()
    payload = dict(data or {})
    if family == FAMILY_IMAGE_GENERATE:
        urls = []
        for key in ("view_url", "url"):
            value = payload.get(key)
            if isinstance(value, str) and value.startswith("/") or (
                isinstance(value, str) and value.startswith("https://")
            ):
                urls.append(value)
        for item in payload.get("assets") or payload.get("version_ids") or []:
            if isinstance(item, dict):
                url = str(item.get("view_url") or item.get("url") or "")
                if url.startswith("/") or url.startswith("https://"):
                    urls.append(url)
        lines = [_user_success_image()]
        for url in urls[:8]:
            lines.append(f"![изображение]({url})")
        return "\n".join(lines)
    return "Готово."


def artifacts_from_tool_data(data: Mapping[str, Any] | None, *, tool_id: str) -> list[dict[str, Any]]:
    payload = dict(data or {})
    out: list[dict[str, Any]] = []
    version_ids = list(payload.get("version_ids") or [])
    if not version_ids and payload.get("version_id"):
        version_ids = [payload.get("version_id")]
    if version_ids:
        for vid in version_ids:
            out.append(
                {
                    "type": "image",
                    "artifact_type": "image",
                    "ref": str(vid),
                    "artifact_id": str(vid),
                    "mime_type": str(payload.get("mime_type") or "image/png"),
                    "view_url": payload.get("view_url") or "",
                }
            )
        return out
    if payload:
        out.append(
            {
                "type": "tool_result",
                "artifact_type": "tool_result",
                "ref": str(payload.get("request_id") or tool_id),
                "artifact_id": str(payload.get("request_id") or tool_id),
            }
        )
    return out


def scene_mentions(task: ActiveTask | None, *stems: str) -> bool:
    if task is None:
        return False
    blob = " ".join(
        str(v)
        for v in (
            task.parameters.get(PARAM_SCENE),
            task.parameters.get(PARAM_STYLE),
            task.goal,
        )
        if v
    )
    return all(_has_stem(blob, (stem,)) for stem in stems)
