#!/usr/bin/env python3
"""One-time, manual, opt-in setup for the managed-agent-runtime POC.

Installs the OpenAI Agents SDK (pinned version, see
``managed_agent_poc.isolated_env.OPENAI_AGENTS_SDK_VERSION``) into an
isolated target directory via ``pip install --target=<dir>`` -- NOT into
this process's/interpreter's own site-packages, and NOT as a virtualenv
(``python3 -m venv`` needs ``ensurepip``, unavailable in some minimal
images; ``--target`` gives the identical isolation property without it).

Run manually (e.g. for local development, or to pre-warm a persistent
cache directory before deploying):

    python3 managed_agent_poc/scripts/setup_isolated_env.py

Never invoked automatically by any test, by ``main.py``, or by any
production startup path -- the managed-agent POC stays fully inert
(and its own ``PANDA_MANAGED_AGENT_POC_ENABLED`` flag stays false) until
someone deliberately opts in via ``PANDA_MANAGED_AGENT_ENABLED``.

This script is no longer the ONLY way the install happens: PR #74's
production-defect fix added ``managed_agent_poc.isolated_env.
ensure_installed()``, a lazy, at-most-once-per-process bootstrap that
``panda_bridge.maybe_respond_via_managed_agent`` calls automatically the
first time a real turn needs it -- so a deployed environment that never
ran this script (the actual production defect this closed) still
self-heals on its own. This script remains useful for pre-warming a
persistent cache directory ahead of time, but is no longer required.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from managed_agent_poc.isolated_env import (  # noqa: E402
    OPENAI_AGENTS_SDK_VERSION,
    _pip_install_target,
    is_installed,
    pkgs_dir,
)


def main() -> int:
    target = pkgs_dir()
    if is_installed():
        print(f"Already installed at {target!r} -- nothing to do.")
        return 0
    print(f"Installing openai-agents=={OPENAI_AGENTS_SDK_VERSION} -> {target}")
    # Same install command ``managed_agent_poc.isolated_env.ensure_installed()``
    # runs lazily in-process -- kept in exactly one place so this manual
    # script and that automatic bootstrap can never drift apart.
    result = _pip_install_target(target)
    if result.returncode != 0:
        print("Install failed.", file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        return result.returncode
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
