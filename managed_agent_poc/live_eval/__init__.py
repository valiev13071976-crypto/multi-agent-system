"""Live semantic evaluation harness for the managed-agent-runtime POC
(``managed_agent_poc``).

This package NEVER runs automatically, is NEVER imported by any
production module, and makes REAL model calls only when explicitly
invoked via ``managed_agent_poc/scripts/run_live_eval.py`` AND
``OPENAI_API_KEY`` is present. It always uses the REAL model path
(``ManagedAgentPOC.run_turn`` with no ``test_scripted_plan``) -- never
``ScriptedModel``, never a hardcoded expected-tool shortcut in the
runtime itself.

The only place natural-language pattern matching exists in this
package is inside ``scoring.py``'s SAFETY-REFUSAL text heuristic, and
that code classifies the MODEL'S OWN OUTPUT for reporting purposes
after the fact -- it never decides which tool the agent calls and is
never part of Panda's or the POC's production tool-selection path. See
``scoring.py``'s module docstring for why this distinction matters and
is not a violation of the "no regex/stem routing" requirement.
"""
