"""Tiro agent runtime (Phase 6 kernel).

Spec: docs/plans/2026-07-06-agent-runtime-spec.md. Provenance is structural:
all data access via AgentContext tools, all model access via ctx.llm — the
context records every read into citations and every call into the trace.
"""
