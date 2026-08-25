import re
from dataclasses import dataclass

GITHUB_TOOL_ID = "github.issue_labels"
OP_ENSURE_PRESENT = "ensure_label_present"
OP_ENSURE_ABSENT = "ensure_label_absent"
GITHUB_OPERATIONS = (OP_ENSURE_PRESENT, OP_ENSURE_ABSENT)
GITHUB_API_BASE = "https://api.github.com"
RESOURCE_PREFIX = "github://"
MAX_LABEL_LENGTH = 50
GITHUB_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class GitHubTargetError(ValueError):
    def __init__(self, error_code: str = "github_target_invalid"):
        self.error_code = error_code
        super().__init__(error_code)


def _validate_name(value: str, field: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 39 or not GITHUB_NAME_RE.match(text):
        raise GitHubTargetError(f"github_invalid_{field}")
    if "/" in text or ":" in text or text in {".", ".."}:
        raise GitHubTargetError(f"github_invalid_{field}")
    lowered = text.lower()
    if lowered.startswith("http") or "github.com" in lowered:
        raise GitHubTargetError(f"github_invalid_{field}")
    return text


def _validate_label(value: str) -> str:
    text = str(value or "")
    if text != text.strip():
        text = text.strip()
    if not text or len(text) > MAX_LABEL_LENGTH:
        raise GitHubTargetError("github_invalid_label")
    if any(ord(char) < 32 for char in text):
        raise GitHubTargetError("github_invalid_label")
    if "\n" in text or "\r" in text or "/" in text:
        raise GitHubTargetError("github_invalid_label")
    return text


def _validate_issue_number(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GitHubTargetError("github_invalid_issue_number")
    if value < 1:
        raise GitHubTargetError("github_invalid_issue_number")
    return value


@dataclass(frozen=True)
class GitHubIssueLabelTarget:
    owner: str
    repo: str
    issue_number: int
    label: str

    def __post_init__(self):
        object.__setattr__(self, "owner", _validate_name(self.owner, "owner"))
        object.__setattr__(self, "repo", _validate_name(self.repo, "repo"))
        object.__setattr__(self, "issue_number", _validate_issue_number(self.issue_number))
        object.__setattr__(self, "label", _validate_label(self.label))

    def repository(self) -> str:
        return f"{self.owner}/{self.repo}".lower()

    def as_resource(self) -> str:
        return (
            f"github://{self.owner}/{self.repo}/issues/"
            f"{self.issue_number}/labels/{self.label}"
        )

    def external_reference(self) -> str:
        return f"github_issue:{self.owner}/{self.repo}#{self.issue_number}"


_RESOURCE_RE = re.compile(
    r"^github://([^/]+)/([^/]+)/issues/(\d+)/labels/(.+)$"
)


def parse_github_label_resource(resource: str) -> GitHubIssueLabelTarget:
    text = str(resource or "")
    if "://" in text and not text.startswith(RESOURCE_PREFIX):
        raise GitHubTargetError("github_resource_host_rejected")
    match = _RESOURCE_RE.match(text)
    if match is None:
        raise GitHubTargetError("github_resource_invalid")
    owner, repo, number, label = match.groups()
    return GitHubIssueLabelTarget(
        owner=owner,
        repo=repo,
        issue_number=int(number),
        label=label,
    )


def label_present(names: tuple[str, ...] | list[str], wanted: str) -> bool:
    wanted_l = wanted.casefold()
    return any(str(name).casefold() == wanted_l for name in names)
