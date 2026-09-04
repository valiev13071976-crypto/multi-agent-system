from config.constants import MAX_CONTEXT_CHARS
from config.config.prompts import (
    CRITIC_PROMPT,
    GENERALIST_PROMPT,
    RESEARCHER_PROMPT,
    RESPONSE_DEPTH_ANALYTICAL,
    RESPONSE_DEPTH_DEEP,
    RESPONSE_DEPTH_DIRECT,
    RESPONSE_DEPTH_NORMAL,
    STRATEGIST_FORMAT,
    STRATEGIST_PROMPT,
    STRATEGIST_ROLE_CORE,
    TECHNICAL_PROMPT,
    TREND_AGENT_PROMPT,
)
from agents.response_depth import (
    DEPTH_ANALYTICAL,
    DEPTH_DEEP,
    DEPTH_DIRECT,
    DEPTH_NORMAL,
    classify_response_depth,
    normalize_response_depth,
)


DEFAULT_ROLE = "strategist"
ROLE_GENERALIST = "generalist"

USER_TASK_MARKER = "USER TASK:"

# Bump when role registry mapping or prompt wiring changes.
ROLE_REGISTRY_VERSION = "1.1.0"
PROMPT_VERSION = "1.1.0"

ALLOWED_ROLE_VALUES = (
    "strategist",
    "critic",
    "researcher",
    "trend_agent",
    "technical",
    "generalist",
)

ROLE_PROMPTS = {
    "strategist": STRATEGIST_PROMPT,
    "critic": CRITIC_PROMPT,
    "researcher": RESEARCHER_PROMPT,
    "trend_agent": TREND_AGENT_PROMPT,
    "technical": TECHNICAL_PROMPT,
    "generalist": GENERALIST_PROMPT,
}

_DEPTH_BLOCKS = {
    DEPTH_DIRECT: RESPONSE_DEPTH_DIRECT,
    DEPTH_NORMAL: RESPONSE_DEPTH_NORMAL,
    DEPTH_ANALYTICAL: RESPONSE_DEPTH_ANALYTICAL,
    DEPTH_DEEP: RESPONSE_DEPTH_DEEP,
}


def role_version_metadata(role_id: str) -> dict:
    """Internal metadata only — not part of public /api/analyze contract."""
    return {
        "role_id": role_id,
        "role_version": ROLE_REGISTRY_VERSION,
        "prompt_version": PROMPT_VERSION,
    }


class InvalidRoleError(ValueError):
    def __init__(self, role):
        self.role = role
        super().__init__(f"Invalid role: {role!r}")


def get_role_prompt(role_id: str) -> str:
    if role_id not in ROLE_PROMPTS:
        raise InvalidRoleError(role_id)
    return ROLE_PROMPTS[role_id]


def _task_category_for_depth(user_request: str) -> str:
    from agents.task_classifier import classify_task

    return classify_task(user_request).category


def instruction_for_role(role_id: str, response_depth: str) -> str:
    """Role expertise plus depth-aware presentation. Does not change role identity."""
    depth = normalize_response_depth(response_depth)
    depth_block = _DEPTH_BLOCKS[depth].strip()
    if role_id == "generalist":
        return f"{GENERALIST_PROMPT.rstrip()}\n\n{depth_block}"
    if role_id == "strategist":
        core = STRATEGIST_ROLE_CORE.rstrip()
        if depth in (DEPTH_ANALYTICAL, DEPTH_DEEP):
            return f"{core}\n{STRATEGIST_FORMAT.rstrip()}\n\n{depth_block}"
        return f"{core}\n\n{depth_block}"
    return f"{get_role_prompt(role_id).rstrip()}\n\n{depth_block}"


def compose_prompt(
    role_id: str,
    user_request: str,
    response_depth: str | None = None,
) -> str:
    if role_id not in ROLE_PROMPTS:
        raise InvalidRoleError(role_id)
    text = user_request if isinstance(user_request, str) else str(user_request)
    depth = response_depth
    if depth is None:
        depth = classify_response_depth(
            text,
            category=_task_category_for_depth(text),
        )
    instruction = instruction_for_role(role_id, depth).rstrip()
    prefix = f"{instruction}\n\n{USER_TASK_MARKER}\n"
    max_user_chars = MAX_CONTEXT_CHARS - len(prefix)
    if max_user_chars < 0:
        return prefix[:MAX_CONTEXT_CHARS]
    if len(text) > max_user_chars:
        text = text[:max_user_chars]
    return prefix + text
