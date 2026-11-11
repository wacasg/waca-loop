"""WACA Loop — organizational AI-governance core engine.

It exposes an MCP server that:
  (A) returns the organization's words  -> grounding (organization terms, hybrid search)
  (B) records AI usage                  -> ledger (Layer-1 usage_log, summary only)
  (C) returns only permitted info       -> retrieval ACL filter (per viewer)

See docs/SPEC_phase1.md. Code references spec sections inline as ``§X``.
"""

__version__ = "1.0.0"
