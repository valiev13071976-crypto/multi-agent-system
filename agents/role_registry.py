from config.constants import MAX_CONTEXT_CHARS
from config.config.prompts import (
    CRITIC_PROMPT,
    RESEARCHER_PROMPT,
    STRATEGIST_PROMPT,
    TECHNICAL_PROMPT,
    TREND_AGENT_PROMPT,
)


DEFAULT_ROLE = "strategist"

USER_TASK_MARKER = "USER TASK:"

ALLOWED_ROLE_VALUES = (
    "strategist",
    "critic",
    "researcher",
    "trend_agent",
    "technical",
)

ROLE_PROMPTS = {
    "strategist": STRATEGIST_PROMPT,
    "critic": CRITIC_PROMPT,
    "researcher": RESEARCHER_PROMPT,
    "trend_agent": TREND_AGENT_PROMPT,
    "technical": TECHNICAL_PROMPT,
}


class InvalidRoleError(ValueError):
    def __init__(self, role):
        self.role = role
        super().__init__(f"Invalid role: {role!r}")


def get_role_prompt(role_id: str) -> str:
    if role_id not in ROLE_PROMPTS:
        raise InvalidRoleError(role_id)
    return ROLE_PROMPTS[role_id]


def compose_prompt(role_id: str, user_request: str) -> str:
    instruction = get_role_prompt(role_id).rstrip()
    prefix = f"{instruction}\n\n{USER_TASK_MARKER}\n"
    max_user_chars = MAX_CONTEXT_CHARS - len(prefix)
    if max_user_chars < 0:
        return prefix[:MAX_CONTEXT_CHARS]
    text = user_request if isinstance(user_request, str) else str(user_request)
    if len(text) > max_user_chars:
        text = text[:max_user_chars]
    return prefix + text
