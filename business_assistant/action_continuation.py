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
from autonomy.capabilities import (
    CAP_FILESYSTEM_WRITE,
    CAP_IMAGE_EDIT,
    CAP_IMAGE_GENERATE,
    CAP_SCRAPE,
)
from business_assistant.follow_up import (
    KIND_NEW_TOPIC,
    KIND_REFERENT,
    KIND_TRANSFORM,
    FollowUpResolution,
)
from business_assistant.intent import requires_business_integration
from security.tenant import require_tenant_id

# Block 5.1: FAMILY_EXCEL now routes to the real, active data_intel capability
# (chat-driven Excel/CSV analysis/transform/compare) instead of the disabled
# excel.inspect stub. TOOL_EXCEL_INSPECT is kept only as a historical alias --
# nothing still routes to it.
TOOL_EXCEL_INSPECT = "excel.inspect"
TOOL_DATA_EXCEL_ASSISTANT = "data.excel_assistant"
TOOL_DATA_COMPARE_WORKBOOKS = "data.compare_workbooks"
TOOL_IMAGE_EDIT = "image.edit"
TOOL_IMAGE_GENERATE = "image.generate"
# Block 5.2: single chat-facing entry point for the Data Acquisition &
# Parsing Platform (see ``acquisition/tools.py``'s ``AcquisitionToolAdapter``).
# Panda -- not the user -- decides fetch vs. crawl inside this one tool.
TOOL_SCRAPE_EXTRACT = "scrape.extract"


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
# Block 5.2: acquisition (scrape/crawl/extract a public web page) -- distinct
# from FAMILY_EXCEL (analyze/transform already-acquired structured data) and
# FAMILY_SEARCH (find pages, never fetch/parse their content). See section 21
# of the Block 5.2 spec: SEARCH != ACQUISITION != CRAWL != DATA INTELLIGENCE.
FAMILY_ACQUISITION = "acquisition"

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
    tool_id=TOOL_DATA_EXCEL_ASSISTANT,
    operation="assist",
    # No statically-required parameter: a fresh spreadsheet attachment OR an
    # inherited ``dataset_id`` from a prior turn each independently satisfy
    # "do we have data to operate on" -- checked explicitly in the FAMILY_EXCEL
    # branch of resolve_action_turn() below, not through the generic
    # required-params gate (which cannot express an OR of two sources).
    required=(),
    optional=("text", "dataset_id"),
    defaults={},
    risk=RISK_READ,
    required_capabilities=(CAP_FILESYSTEM_WRITE,),
    artifact_type="workbook",
)

COMPARE_WORKBOOKS_CONTRACT = CapabilityContract(
    family=FAMILY_EXCEL,
    tool_id=TOOL_DATA_COMPARE_WORKBOOKS,
    operation="compare_workbooks",
    required=(),
    optional=("text",),
    defaults={},
    risk=RISK_READ,
    required_capabilities=(CAP_FILESYSTEM_WRITE,),
    artifact_type="workbook",
)

ACQUISITION_CONTRACT = CapabilityContract(
    family=FAMILY_ACQUISITION,
    tool_id=TOOL_SCRAPE_EXTRACT,
    operation="extract",
    # The URL is deterministically extracted from the user's message BEFORE
    # the task is created (see ``_extract_url`` / the FAMILY_ACQUISITION
    # branch of ``resolve_action_turn``) and stamped into ``task.parameters``
    # up front, so this "required" field is satisfied at creation time rather
    # than through a generic missing-params gate -- mirrors how FAMILY_EXCEL's
    # dataset source (attachment OR inherited dataset_id) is resolved.
    required=("url",),
    optional=("max_pages", "extraction_plan"),
    defaults={},
    risk=RISK_READ,
    required_capabilities=(CAP_SCRAPE,),
    artifact_type="dataset",
)

CONTRACTS: dict[str, CapabilityContract] = {
    FAMILY_IMAGE_GENERATE: IMAGE_GENERATE_CONTRACT,
    FAMILY_IMAGE_EDIT: IMAGE_EDIT_CONTRACT,
    FAMILY_EXCEL: EXCEL_CONTRACT,
    FAMILY_ACQUISITION: ACQUISITION_CONTRACT,
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

# Block 5.2: a bare http(s) URL in the message is the strongest, fully
# deterministic acquisition signal -- mirrors how a spreadsheet attachment is
# the strongest Excel signal (spec section 20: "no technical mode picker").
_URL_RE = re.compile(r"https?://[^\s<>\"'()\[\]]+", re.I)
_PAGES_COUNT_RE = re.compile(r"(\d{1,3})\s*(?:страниц\w*|pages?)", re.I)
MAX_ACQUISITION_PAGES = 200

# Weaker fallback signal than a URL: explicit "go acquire/parse" verbs with no
# URL yet (e.g. referencing "this page" from earlier context) still route to
# FAMILY_ACQUISITION so the turn resolver asks for the missing URL instead of
# silently answering conversationally (spec section 39: "ask one useful
# clarification").
_ACQUISITION_INTENT_STEMS = (
    "собери",
    "вытащи",
    "извлеки",
    "спарси",
    "спарсь",
    "парсинг",
    "scrape",
    "crawl",
)


def _extract_url(text: str) -> str:
    match = _URL_RE.search(text or "")
    if not match:
        return ""
    return match.group(0).rstrip(".,;:!?)]}\u00bb\"'")


def _extract_max_pages(text: str) -> int:
    match = _PAGES_COUNT_RE.search(text or "")
    if not match:
        return 1
    try:
        value = int(match.group(1))
    except ValueError:
        return 1
    return max(1, min(value, MAX_ACQUISITION_PAGES))


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


def detect_family(
    text: str, active: ActiveTask | None, *, has_spreadsheet_attachment: bool = False
) -> str | None:
    raw = text or ""
    if _has_stem(raw, _WRITE_STEMS):
        return FAMILY_WRITE
    # Block 5.1 chat integration (spec section 12): the user never selects an
    # "Excel mode" -- attaching a spreadsheet is itself the strongest, fully
    # deterministic signal, regardless of the accompanying wording.
    if has_spreadsheet_attachment:
        return FAMILY_EXCEL
    # Block 5.2: an explicit URL always wins over any active task -- pasting
    # a new link is a deliberate "acquire this" signal (spec section 20),
    # stronger than a generic active-family continuation heuristic.
    if _extract_url(raw):
        return FAMILY_ACQUISITION
    if _has_stem(raw, _ACQUISITION_INTENT_STEMS):
        return FAMILY_ACQUISITION
    if _has_stem(raw, _EXCEL_STEMS) and (
        _has_stem(raw, ("анализ", "analyze", "inspect", "проанализ")) or _has_stem(raw, _MAKE_STEMS)
    ):
        return FAMILY_EXCEL
    if active is not None and active.family == FAMILY_EXCEL:
        # Block 5.1 multi-turn continuation (spec section 13): legitimate
        # follow-ups ("Оставь Samsung", "Только дешевле 50000", "Минус 12%",
        # "Сохрани Excel") are short deterministic data commands that often
        # ARE business/data-domain keywords ("прайс", "samsung", "excel")
        # themselves -- unlike image continuation, domain-keyword presence
        # must not be treated as evidence of an unrelated new task here.
        # Only a genuinely different top-level ask (weather/general trivia)
        # breaks continuation.
        if not (_has_stem(raw, _WEATHER_STEMS) or _has_stem(raw, _QUESTION_NEW_STEMS)):
            return FAMILY_EXCEL
    if active is not None and active.family == FAMILY_ACQUISITION:
        # Continuation while still waiting for the URL (clarification asked
        # last turn) -- keep the frame alive for a plain follow-up reply.
        if not (_has_stem(raw, _WEATHER_STEMS) or _has_stem(raw, _QUESTION_NEW_STEMS)):
            return FAMILY_ACQUISITION
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
    has_spreadsheet_attachment: bool = False,
) -> str:
    if active is None:
        return NEW_TASK
    family = detect_family(text, active, has_spreadsheet_attachment=has_spreadsheet_attachment)
    if follow_up is not None and follow_up.kind in {KIND_TRANSFORM, KIND_REFERENT}:
        if not family:
            return NEW_TASK
    # Block 5.1: an Excel-continuation turn commonly contains business/data
    # keywords ("прайс", "samsung", "excel") that would otherwise look like
    # an unrelated new top-level business-integration task (see
    # requires_business_integration._BUSINESS_TASK_KEYWORDS) -- once
    # detect_family has already deterministically resolved this turn to the
    # active Excel task, that heuristic must not override it.
    if (
        family not in {FAMILY_EXCEL, FAMILY_ACQUISITION}
        and _is_unrelated_new_task(text)
        and not _is_quantity_only(text)
    ):
        return NEW_TASK
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
    if family == FAMILY_ACQUISITION:
        return "Сейчас не могу собрать данные со страницы — возможность недоступна."
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
    # Block 5.1: deterministic count of THIS turn's attachments already
    # resolved (trusted, tenant/conversation-verified) to kind=="spreadsheet"
    # -- never a raw/unresolved ref count. 0 for every pre-5.1 caller/test
    # (default), so existing image/document/search routing is unaffected.
    spreadsheet_attachment_count: int = 0,
) -> ActionDecision:
    """Pure-ish turn resolver. At most one extra LLM call: never (extra_llm=False)."""
    current = (text or "").strip()
    tenant = require_tenant_id(tenant_id)
    owner = str(owner_id or "")
    conv = str(conversation_id or "")
    active = store.get(tenant_id=tenant, owner_id=owner, conversation_id=conv)
    has_spreadsheet_attachment = spreadsheet_attachment_count > 0

    if follow_up is not None and follow_up.kind in {KIND_TRANSFORM, KIND_REFERENT}:
        resolved_family = (
            detect_family(current, active, has_spreadsheet_attachment=has_spreadsheet_attachment)
            if active is not None
            else None
        )
        excel_continuation = (
            active is not None and active.family == FAMILY_EXCEL and resolved_family == FAMILY_EXCEL
        )
        image_continuation = (
            active is not None
            and resolved_family == active.family
            and _is_image_artifact_request(current)
        )
        if not (excel_continuation or image_continuation):
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

    mode = continuation_decision(
        current,
        active=active,
        follow_up=follow_up,
        has_spreadsheet_attachment=has_spreadsheet_attachment,
    )
    family = detect_family(
        current,
        active if mode != NEW_TASK else None,
        has_spreadsheet_attachment=has_spreadsheet_attachment,
    )

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
        mode == NEW_TASK
        and _is_unrelated_new_task(current)
        and family not in {FAMILY_IMAGE_GENERATE, FAMILY_EXCEL, FAMILY_ACQUISITION}
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

    if family == FAMILY_ACQUISITION:
        is_new = mode == NEW_TASK or active is None or active.family != FAMILY_ACQUISITION
        if is_new:
            if active is not None:
                active.status = STATUS_SUPERSEDED
                store.put(active)
            task = ActiveTask(
                task_id=str(uuid.uuid4()),
                tenant_id=tenant,
                owner_id=owner,
                conversation_id=conv,
                family=FAMILY_ACQUISITION,
                tool_id=ACQUISITION_CONTRACT.tool_id,
                operation=ACQUISITION_CONTRACT.operation,
                goal=current,
                parameters={
                    "url": _extract_url(current),
                    "max_pages": _extract_max_pages(current),
                },
                artifact_type=ACQUISITION_CONTRACT.artifact_type,
                status=STATUS_DRAFT,
                risk=RISK_READ,
            )
        else:
            task = active
            if task.status in {STATUS_COMPLETED, STATUS_FAILED_RETRYABLE}:
                task.status = STATUS_DRAFT
            new_url = _extract_url(current)
            if new_url:
                task.parameters["url"] = new_url
                task.parameters["max_pages"] = _extract_max_pages(current)
        task.goal = current

        url = str(task.parameters.get("url") or "")
        if not url:
            task.missing_required = ("url",)
            task.status = STATUS_WAITING_FOR_INPUT
            store.put(task)
            return ActionDecision(
                decision=ASK_CLARIFICATION,
                readiness=NEEDS_REQUIRED_INPUT,
                continuation=CONTINUE_ACTIVE_TASK if not is_new else NEW_TASK,
                task=task,
                user_message="Пришлите ссылку на страницу, которую нужно собрать/разобрать.",
                extra_llm=False,
            )
        task.missing_required = ()

        args: dict[str, Any] = {
            "url": url,
            "max_pages": int(task.parameters.get("max_pages") or 1),
            "conversation_id": conv,
        }

        cap_status = inspect_capability(gateway, ACQUISITION_CONTRACT.tool_id)
        if cap_status != CAPABILITY_AVAILABLE_AND_AUTHORIZED:
            task.status = STATUS_FAILED_RETRYABLE
            store.put(task)
            return ActionDecision(
                decision=FAIL_UNAVAILABLE,
                readiness=NOT_EXECUTABLE,
                continuation=CONTINUE_ACTIVE_TASK if not is_new else NEW_TASK,
                task=task,
                arguments=args,
                user_message=user_unavailable_message(FAMILY_ACQUISITION),
                tool_id=ACQUISITION_CONTRACT.tool_id,
                operation=ACQUISITION_CONTRACT.operation,
                extra_llm=False,
                capability_status=cap_status,
            )

        idem = _idempotency_key(request_id, ACQUISITION_CONTRACT.tool_id, args)
        task.status = STATUS_READY
        store.put(task)
        return ActionDecision(
            decision=CALL_TOOL,
            readiness=READY_TO_EXECUTE,
            continuation=CONTINUE_ACTIVE_TASK if not is_new else NEW_TASK,
            task=task,
            arguments=args,
            tool_id=ACQUISITION_CONTRACT.tool_id,
            operation=ACQUISITION_CONTRACT.operation,
            extra_llm=False,
            capability_status=cap_status,
            idempotency_key=idem,
        )

    if family == FAMILY_EXCEL:
        is_new = mode == NEW_TASK or active is None or active.family != FAMILY_EXCEL
        if is_new:
            if active is not None:
                active.status = STATUS_SUPERSEDED
                store.put(active)
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
                status=STATUS_DRAFT,
                risk=RISK_READ,
            )
        else:
            task = active
            if task.status in {STATUS_COMPLETED, STATUS_FAILED_RETRYABLE}:
                task.status = STATUS_DRAFT
        task.goal = current

        # Section 13 (multi-turn continuation): a new attachment always wins
        # over any inherited dataset_id (the user is deliberately switching
        # data). Otherwise resolve the prior dataset so the user never has to
        # re-upload/re-state which file to operate on.
        inherited_dataset_id = str(task.parameters.get("dataset_id") or "")
        has_dataset = bool(inherited_dataset_id) and spreadsheet_attachment_count <= 0
        if spreadsheet_attachment_count <= 0 and not inherited_dataset_id:
            task.missing_required = (PARAM_FILE,)
            task.status = STATUS_WAITING_FOR_INPUT
            store.put(task)
            return ActionDecision(
                decision=ASK_CLARIFICATION,
                readiness=NEEDS_REQUIRED_INPUT,
                continuation=CONTINUE_ACTIVE_TASK if not is_new else NEW_TASK,
                task=task,
                user_message="Приложите файл Excel/CSV, чтобы я мог его обработать.",
                extra_llm=False,
            )

        # Section 9/12 (reconciliation, chat integration): two spreadsheet
        # attachments in the same turn is the deterministic signal for the
        # two-workbook comparison capability -- the user never picks a
        # separate "compare mode".
        if spreadsheet_attachment_count >= 2:
            contract = COMPARE_WORKBOOKS_CONTRACT
        else:
            contract = EXCEL_CONTRACT
        task.tool_id = contract.tool_id
        task.operation = contract.operation
        task.missing_required = ()

        args: dict[str, Any] = {"text": current}
        if has_dataset:
            args["dataset_id"] = inherited_dataset_id

        cap_status = inspect_capability(gateway, contract.tool_id)
        if cap_status != CAPABILITY_AVAILABLE_AND_AUTHORIZED:
            task.status = STATUS_FAILED_RETRYABLE
            store.put(task)
            return ActionDecision(
                decision=FAIL_UNAVAILABLE,
                readiness=NOT_EXECUTABLE,
                continuation=CONTINUE_ACTIVE_TASK if not is_new else NEW_TASK,
                task=task,
                arguments=args,
                user_message=user_unavailable_message(FAMILY_EXCEL),
                tool_id=contract.tool_id,
                operation=contract.operation,
                extra_llm=False,
                capability_status=cap_status,
            )

        idem = _idempotency_key(request_id, contract.tool_id, args)
        task.status = STATUS_READY
        store.put(task)
        return ActionDecision(
            decision=CALL_TOOL,
            readiness=READY_TO_EXECUTE,
            continuation=CONTINUE_ACTIVE_TASK if not is_new else NEW_TASK,
            task=task,
            arguments=args,
            tool_id=contract.tool_id,
            operation=contract.operation,
            extra_llm=False,
            capability_status=cap_status,
            idempotency_key=idem,
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


def format_tool_user_text(
    *,
    family: str,
    data: Mapping[str, Any] | None,
    success: bool,
    artifacts: list[dict[str, Any]] | None = None,
) -> str:
    if not success:
        return _user_tool_error()
    payload = dict(data or {})
    if family == FAMILY_EXCEL:
        status = str(payload.get("status") or "OK")
        if status == "AMBIGUOUS":
            msg = str(payload.get("message_safe") or "Уточните запрос.")
            candidates = list(payload.get("candidates") or [])
            if candidates:
                msg += " Варианты: " + ", ".join(str(c) for c in candidates) + "."
            return msg
        if status == "NEEDS_USER_MAPPING":
            return str(payload.get("message_safe") or "Не удалось сопоставить столбцы для сравнения.")
        if status == "BATCH_QUEUED":
            return str(payload.get("summary_text") or "Файл большой — обрабатываю в фоне.")
        lines: list[str] = []
        summary_text = str(payload.get("summary_text") or "").strip()
        if summary_text:
            lines.append(summary_text)
        workbook = payload.get("workbook")
        if isinstance(workbook, dict) and workbook.get("view_url"):
            lines.append(f"[Скачать Excel]({workbook['view_url']})")
        if not lines:
            lines.append("Готово.")
        return "\n".join(lines)
    if family == FAMILY_ACQUISITION:
        status = str(payload.get("status") or "OK")
        if status == "BATCH_QUEUED":
            return str(
                payload.get("summary_text")
                or "Собираю данные — задача большая, выполняю в фоне."
            )
        record_count = int(payload.get("record_count") or 0)
        if record_count:
            return f"Собрал {record_count} записей со страницы."
        return "Не нашёл структурированных данных на странице."
    if family in (FAMILY_IMAGE_GENERATE, FAMILY_IMAGE_EDIT):
        urls: list[str] = []
        seen: set[str] = set()

        def _add(url: str) -> None:
            if (url.startswith("/") or url.startswith("https://")) and url not in seen:
                seen.add(url)
                urls.append(url)

        # Production acceptance defect closure: prefer the caller-supplied,
        # already-registered ``artifacts`` list (each item's view_url has
        # been rewritten to the canonical /artifacts/{id}/view route once
        # register_external_image() runs) so the embedded markdown link and
        # the canonical artifact_id the UI needs for direct Edit/Download
        # actions always point at the exact same resource. Falls back to the
        # raw tool payload for callers that never registered artifacts
        # (existing tests/behavior unchanged).
        if artifacts:
            for item in artifacts:
                if isinstance(item, dict) and str(item.get("artifact_type") or item.get("type") or "") == "image":
                    _add(str(item.get("view_url") or ""))
        else:
            # Prefer the per-item "assets" list (one dict per generated variant,
            # each with its OWN view_url); it is a superset of the top-level
            # "view_url"/"url" convenience mirror (== the first asset's URL).
            # Consulting BOTH sources unconditionally previously double-added the
            # first image's URL -- rendering the same generated image twice in a
            # single-variant response. Bare "version_ids" (plain id strings, no
            # per-item URL) fall back to the single top-level view_url/url.
            assets = payload.get("assets")
            if isinstance(assets, list) and assets:
                for item in assets:
                    if isinstance(item, dict):
                        _add(str(item.get("view_url") or item.get("url") or ""))
            else:
                for key in ("view_url", "url"):
                    value = payload.get(key)
                    if isinstance(value, str):
                        _add(value)
        lines = [_user_success_image()]
        for url in urls[:8]:
            lines.append(f"![изображение]({url})")
        return "\n".join(lines)
    return "Готово."


def artifacts_from_tool_data(data: Mapping[str, Any] | None, *, tool_id: str) -> list[dict[str, Any]]:
    payload = dict(data or {})
    out: list[dict[str, Any]] = []
    # Block 5.1: a generated workbook (data.excel_assistant/data.compare_workbooks,
    # only present when execute_nl_request()'s plan wanted an export, or a
    # workbook comparison succeeded) -- registered through the canonical
    # ArtifactService by data_intel.service, so artifact_id/view_url here are
    # already the real, authorized ones (never a raw internal blob ref).
    workbook = payload.get("workbook")
    if isinstance(workbook, dict) and workbook.get("artifact_id"):
        return [
            {
                "type": "workbook",
                "artifact_type": "workbook",
                "ref": str(workbook.get("artifact_id")),
                "artifact_id": str(workbook.get("artifact_id")),
                "mime_type": str(workbook.get("mime_type") or ""),
                "view_url": str(workbook.get("view_url") or ""),
            }
        ]
    # Prefer the per-item "assets" list (ProductMediaToolAdapter always provides one) so
    # each artifact gets its OWN view_url/mime_type. The version_ids-only fallback below
    # previously reused the single top-level "view_url" (the first generated image) for
    # every version_id -- correct for variant_count=1, but silently wrong for >1 variants
    # (every additional generated image pointed at the first image's URL).
    assets = payload.get("assets")
    if isinstance(assets, list) and assets:
        for item in assets:
            if not isinstance(item, dict):
                continue
            vid = str(item.get("version_id") or item.get("ref") or "")
            if not vid:
                continue
            out.append(
                {
                    "type": "image",
                    "artifact_type": str(item.get("artifact_type") or "image"),
                    "ref": vid,
                    "artifact_id": vid,
                    "mime_type": str(item.get("mime_type") or payload.get("mime_type") or "image/png"),
                    "view_url": str(item.get("view_url") or ""),
                }
            )
        if out:
            return out
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
    # Block 5.1: data_intel tools (analyze/filter/ambiguous/batch-queued
    # results) legitimately have NO artifact at all -- the generic
    # "tool_result" pseudo-artifact fallback below exists for other
    # capabilities' UI needs and must not manufacture a fake, non-downloadable
    # "artifact" here (would wrongly look like a generated file, and would
    # get persisted into ActiveTask.last_artifact_ids).
    if tool_id in {TOOL_DATA_EXCEL_ASSISTANT, TOOL_DATA_COMPARE_WORKBOOKS, TOOL_SCRAPE_EXTRACT}:
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
