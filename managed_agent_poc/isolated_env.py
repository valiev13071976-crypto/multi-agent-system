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
"""

from __future__ import annotations

import os

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
        f"POC. This never runs automatically -- production startup never "
        f"needs it."
    )
