"""PANDA — MANAGED AGENT RUNTIME POC (experimental, disabled by default).

Proves whether Panda can stop growing custom natural-language routing /
regex intent trees (``is_explicit_*``, ``_wants_*``, stem dictionaries --
see ``business_assistant/action_continuation.py`` and
``data_intel/service.py``) and instead delegate SEMANTIC tool selection
and multi-turn orchestration to a modern managed agent runtime (the
OpenAI Agents SDK), while Panda itself remains the business-control layer
(tenants, permissions, approvals, audit, budgets, Bitrix/Aspro schema,
pricing/publication policy).

THIS PACKAGE IS A POC. It is never imported by ``main.py`` or any
existing Panda runtime module (``RouterV2``,
``WorkflowPandaConversationGateway``, the existing Data Intelligence
service, the existing Bitrix workflow, ``ToolGateway``, the existing
durable execution stores) -- it lives entirely BESIDE them. Nothing here
changes production behavior merely by being imported: every entry point
additionally checks ``managed_agent_poc.flags.is_enabled()``
(``PANDA_MANAGED_AGENT_POC_ENABLED``, default ``false``) and raises
before doing any real work when the flag is off.

Why an isolated subprocess (see ``managed_agent_poc/runtime_subprocess.py``
and ``managed_agent_poc/adapter.py``), instead of importing the OpenAI
Agents SDK directly into this process:

Panda already owns a top-level package literally named ``agents/``
(``agents/router_v2.py``, ``agents/openai_agent.py``, etc.). The OpenAI
Agents SDK's PyPI distribution is ``openai-agents``, but its Python
IMPORT name is also ``agents`` (``from agents import Agent, Runner,
function_tool``). Installing it into this process's normal environment
would collide with Panda's own ``agents`` package on ``sys.path`` --
either shadowing Panda's package (breaking
``agents.router_v2``/``agents.openai_agent`` imports everywhere) or being
shadowed itself (breaking ``from agents import Agent`` for the SDK),
depending on path order -- a real, load-bearing production import either
way. This is proven, not assumed: see the version/collision findings in
this POC's PR description.

The isolated subprocess (a separate Python process whose ``sys.path`` is
built explicitly, never inheriting this process's ``sys.path``) resolves
this cleanly with zero changes to Panda's existing ``agents/`` package:
the SDK's packages directory is inserted BEFORE the repo root, so
``import agents`` in the subprocess always resolves to the SDK, while
this (parent) process never imports the SDK at all and keeps resolving
``agents`` to Panda's own package exactly as it does today.
"""
