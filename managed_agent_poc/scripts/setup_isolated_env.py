#!/usr/bin/env python3
"""One-time, manual, opt-in setup for the managed-agent-runtime POC.

Installs the OpenAI Agents SDK (pinned version, see
``managed_agent_poc.isolated_env.OPENAI_AGENTS_SDK_VERSION``) into an
isolated target directory via ``pip install --target=<dir>`` -- NOT into
this process's/interpreter's own site-packages, and NOT as a virtualenv
(``python3 -m venv`` needs ``ensurepip``, unavailable in some minimal
images; ``--target`` gives the identical isolation property without it).

Run manually:

    python3 managed_agent_poc/scripts/setup_isolated_env.py

Never invoked automatically by any test, by ``main.py``, or by any
production startup path -- the managed-agent POC stays fully inert
(and its own ``PANDA_MANAGED_AGENT_POC_ENABLED`` flag stays false) until
someone deliberately runs this script AND opts in.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from managed_agent_poc.isolated_env import (  # noqa: E402
    OPENAI_AGENTS_SDK_VERSION,
    is_installed,
    pkgs_dir,
)


def main() -> int:
    target = pkgs_dir()
    if is_installed():
        print(f"Already installed at {target!r} -- nothing to do.")
        return 0
    Path(target).mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--quiet",
        f"--target={target}",
        f"openai-agents=={OPENAI_AGENTS_SDK_VERSION}",
    ]
    print(f"Installing openai-agents=={OPENAI_AGENTS_SDK_VERSION} -> {target}")
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        print("Install failed.", file=sys.stderr)
        return result.returncode
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
