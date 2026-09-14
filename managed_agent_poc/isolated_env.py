"""Locates the isolated OpenAI Agents SDK install used ONLY by the
managed-agent-runtime POC subprocess (see ``runtime_subprocess.py``).

Deliberately NOT a virtualenv (``python3 -m venv`` requires ``ensurepip``,
unavailable in some minimal images) -- ``pip install --target=<dir>``
achieves the identical isolation property (a self-contained package
directory, no interpreter/site-packages mutation) without that
dependency. See ``scripts/setup_managed_agent_poc_env.py`` to create it.

Default location is OUTSIDE the repository (``/tmp`` by default) so it
is never accidentally committed to git and never collides with
anything under ``/workspace``. Override with
``PANDA_MANAGED_AGENT_POC_PKGS_DIR`` if a different location is
preferred (e.g. a persistent cache directory in a real deployment).

Production defect closure (PR #74 integration -- ``PANDA_MANAGED_
AGENT_ENABLED=true`` in a real deployed environment silently never
entered the managed-agent path): the module docstring below used to say
"this never runs automatically -- production startup never needs it."
That was true only while the outer integration flag stayed off. Once an
operator turns ``PANDA_MANAGED_AGENT_ENABLED`` on in a freshly deployed
container (Railway/Nixpacks: a brand-new filesystem built from
``requirements.txt`` only -- nothing in the build or start command ever
runs ``scripts/setup_isolated_env.py``), ``is_installed()`` is
unconditionally ``False`` on every single request, so
``managed_agent_poc.adapter.ManagedAgentPOC.real_model_available()``
always reports unavailable and ``panda_bridge.maybe_respond_via_
managed_agent`` always returns ``None`` -- the exact "flag is true but
still gets the old generic Excel analysis" symptom. ``ensure_installed()``
below closes this by making the SAME one-time install self-healing:
attempted lazily, at most once per process, only when
``panda_bridge`` actually needs it (i.e. only when the outer flag is on
AND a turn is eligible) -- never at import time, never unconditionally,
never a second time in the SAME process once it has succeeded or failed
once so a persistently network-isolated deployment never pays a retry
cost on every turn.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading

logger = logging.getLogger(__name__)

PKGS_DIR_ENV_VAR = "PANDA_MANAGED_AGENT_POC_PKGS_DIR"
DEFAULT_PKGS_DIR = "/tmp/panda_managed_agent_poc_pkgs"

# Pinned exact version -- report this number verbatim in the POC writeup;
# bump deliberately, never silently, if the POC is ever revisited.
OPENAI_AGENTS_SDK_VERSION = "0.22.2"


def pkgs_dir() -> str:
    return str(os.environ.get(PKGS_DIR_ENV_VAR) or DEFAULT_PKGS_DIR)


def is_installed() -> bool:
    directory = pkgs_dir()
    marker = os.path.join(directory, "agents", "__init__.py")
    return os.path.isfile(marker)


def describe_unavailable() -> str:
    return (
        f"OpenAI Agents SDK ({OPENAI_AGENTS_SDK_VERSION}) is not installed at "
        f"{pkgs_dir()!r}. Run "
        f"`python3 managed_agent_poc/scripts/setup_isolated_env.py` once "
        f"(requires network access to PyPI) before using the managed-agent "
        f"POC, or let `ensure_installed()` bootstrap it lazily on first use."
    )


def _pip_install_target(target: str, *, timeout_s: float = 90.0) -> subprocess.CompletedProcess:
    """The ONE install command both the manual setup script and the lazy
    ``ensure_installed()`` bootstrap below run -- kept in exactly one place
    so the two call sites can never drift (same package, same pinned
    version, same ``--target`` isolation)."""
    os.makedirs(target, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--quiet",
        f"--target={target}",
        f"openai-agents=={OPENAI_AGENTS_SDK_VERSION}",
    ]
    return subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=timeout_s)


_bootstrap_lock = threading.Lock()
# Keyed by target directory (not a single global bool) so tests pointing
# PANDA_MANAGED_AGENT_POC_PKGS_DIR at different scratch directories each
# get their own independent bootstrap attempt/outcome.
_bootstrap_attempted: dict[str, bool] = {}


def ensure_installed(*, timeout_s: float = 90.0) -> bool:
    """Idempotent, lazy, at-most-once-per-process-per-directory bootstrap
    of the isolated OpenAI Agents SDK install (see module docstring for
    why this exists). Safe to call on every eligible turn: a no-op
    (``is_installed()`` short-circuit, no subprocess) once installed, and
    never retried within the same process after one failed attempt for
    the same directory -- so a deployment with no PyPI egress degrades to
    exactly the prior fail-open behavior (fast ``False`` every time)
    instead of retrying a slow network call per request. Never raises;
    logs only non-secret diagnostic text (package name/version/target
    path, pip's own stdout/stderr -- never touches, reads, or logs any
    environment variable value)."""
    if is_installed():
        return True
    target = pkgs_dir()
    with _bootstrap_lock:
        if is_installed():
            return True
        if target in _bootstrap_attempted:
            return _bootstrap_attempted[target] and is_installed()
        ok = False
        try:
            logger.info(
                "managed_agent_poc: bootstrapping isolated OpenAI Agents SDK "
                "install (openai-agents==%s -> %s); this runs at most once "
                "per process.",
                OPENAI_AGENTS_SDK_VERSION,
                target,
            )
            result = _pip_install_target(target, timeout_s=timeout_s)
            ok = result.returncode == 0 and is_installed()
            if ok:
                logger.info("managed_agent_poc: isolated OpenAI Agents SDK install succeeded at %s", target)
            else:
                logger.warning(
                    "managed_agent_poc: isolated OpenAI Agents SDK install failed "
                    "(exit=%s); managed-agent turns will keep falling back to the "
                    "legacy conversational path until this succeeds on a future "
                    "process start. pip_stderr_tail=%r",
                    result.returncode,
                    (result.stderr or "")[-500:],
                )
        except Exception:
            logger.exception("managed_agent_poc: isolated OpenAI Agents SDK bootstrap install raised")
            ok = False
        _bootstrap_attempted[target] = ok
        return ok
