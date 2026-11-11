# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## Working rules

1. **Develop on a branch, merge through a pull request.** `main` is protected: do not push to it
   directly; the pull request must pass CI (public-content check, ruff, pytest).
2. **No real organization data in the repository** — no real names, e-mail addresses, production
   project ids, URLs or spreadsheet ids, in code, tests or docs. Tests use the fictional
   organization in `tests/org.test.toml`; examples use `example.org`. `org.toml` and
   `members.json` are git-ignored and must never be committed.
3. **The repository is public — write no development history.** Comments, docs and commit
   messages state the current behavior and its reason in the present tense: no dates, reviewer
   names, finding severities, internal approval trails or references to documents outside the
   repo. Run `python scripts/check_public_content.py` before every push (it also runs in CI and
   in `tests/test_public_content.py`; a git-ignored `.public-denylist` adds optional word checks).
4. **Anything that touches live resources needs the operator's explicit approval first**:
   BigQuery writes, GCP changes, deploys, and creating issues/PRs on someone's behalf.
5. When WACA Loop itself is connected to your session, follow the operating rules in
   `docs/SPEC_ops.md` §6 (check `get_applicable_policy` before answering, `log_usage` after,
   summaries only, and declare `set_session_scope` external/private for work outside the
   organization).

## What this is

WACA Loop is an organizational AI-governance engine exposed as an **MCP server**. A member's AI
client connects to one server that: (A) returns the organization's vocabulary (hybrid search),
(B) records AI usage to a Layer-1 ledger (summaries only), (C) returns only information the viewer
is permitted to see (ACL-preserving retrieval), (D) delivers the organization's AI policy
(`get_applicable_policy`), and (E) serves the SFAQ judgment-knowledge layer (`search_sfaq` etc.).
Plus a 4D output-signature system prompt and a positive-list "shadow AI" monitor.

**Everything organization-specific lives in `org.toml`** (`src/waca_loop/org.py`): the org name,
Google Workspace domains, the unit ids with special meaning (org-wide / steward / board / owner /
export_admin), the words that mark a conversation as the organization's work, and the sheet-label →
unit-id map. Never hard-code an organization's name, domain or unit ids in code — read them from
`get_org_config()` (or the unit constants in `retrieval/acl.py`). Entry points that touch real data
call `exit_if_incomplete()` and refuse to run while `org.toml` is missing or holds `CHANGE-ME`.

**Deployment**: hosted remote MCP on Cloud Run — `docs/SETUP_gcp.md` §8. The server is **its own
OAuth authorization server** (`src/waca_loop/auth/as_provider.py`): MCP clients
self-register via DCR, Google login is brokered behind our `/authorize`, and the
`oauth_identity_map` sub-allowlist gates access at the Google callback (unregistered sub →
rejected, never a visitor). Access/refresh tokens are self-contained HMAC-signed (`auth/tokens.py`,
key = `MCP_TOKEN_SIGNING_KEY`) and DCR clients persist in BigQuery `oauth_clients`
(`auth/client_store.py`); only in-flight login state is in-memory → **Cloud Run must run exactly
1 instance**.

The authoritative design is `docs/SPEC_phase1.md` (+ `SPEC_phase2.md` for SFAQ, `SPEC_phase3.md`
for ERN). **Code references spec sections inline as `§X`** (e.g. `§3.3`); when changing behavior,
find the matching `§` in the spec first — the spec is the contract. Acceptance criteria T1–T10 (§8)
map 1:1 to `tests/test_t01..t10`; SFAQ criteria S1–S9 (`SPEC_phase2.md`) to `tests/test_sfaq*.py`;
ERN criteria E1–E13 (`docs/ERN_ACCEPTANCE_TESTS.md`) to `tests/test_e*.py`. (`tests/test_s01..s09` are the
survey/improvement-loop tests — a separate series.)

## Commands

```bash
pip install -e '.[dev]'        # install (editable) + dev tools
pytest -q                       # full test suite (offline, no BigQuery/OAuth needed)
pytest tests/test_t02_acl_block.py            # one test file
ruff check .                    # lint (must stay clean)
ruff check . --fix              # autofix

python scripts/init_bigquery.py --dry-run     # print resolved DDL (needs .env; placeholder OAuth values are fine)
ORG_CONFIG=sample/org.toml python scripts/register_members.py sample/members.json   # dry-run
waca-loop                       # MCP server over stdio (needs .env + org.toml)
```

`make` shortcuts wrap these: `make setup|test|lint|fmt|ddl-dry|backfill-dry|serve`.
Tests must pass without any cloud credentials; `tests/conftest.py` points `ORG_CONFIG` at
`tests/org.test.toml` before anything is imported.

**Project subagents** live in `.claude/agents/` — `spec-guardian` (invariant/§X review) and
`acl-privacy-auditor` (leak hunt) review a diff read-only; `store-builder`, `mcp-tool-builder`,
`test-author` and `docs-scribe` do pattern-bound implementation. Run the two reviewers before any
change that touches `src/` or `sql/`.

## Architecture — the load-bearing ideas

**Pure logic vs. live I/O, enforced by imports.** The core (`models`, `org`, `embeddings`,
`retrieval/*`, `signature/*`, `monitor/*`) depends only on the stdlib. Anything touching BigQuery,
OAuth, or MCP is **lazy-imported inside functions** (see `bq.py`, `grounding/*`, `ledger/*`,
`auth/oauth.py`, `server.py:build_mcp`). This is why the suite runs offline. **Do not add a
top-level `import google.cloud...` / `import mcp` to a pure-logic module.**

**Store pattern: every data backend has an InMemory + BigQuery pair** with the same surface
(`grounding/terms.py`, `grounding/persons.py`, `ledger/usage_log.py`, …). Tests inject the InMemory
ones via `tests/conftest.py`; `server.build_production_context()` wires the BigQuery ones. Add a
new query? Implement it on **both** so tests stay offline.

**Tool logic lives on `ServerContext`, not in MCP handlers.** `server.py` keeps every tool's
behavior as plain methods on `ServerContext` (directly unit-testable); `build_mcp()` is a thin
binding onto FastMCP. Test/extend tools by calling `ServerContext` methods.

**Hosted mode: one shared runtime, per-request viewer.** `SharedRuntime` holds viewer-independent
singletons; viewer-bound stores are built per request via factories (`term_store_factory`,
`knowledge_store_factory`) because the BigQuery stores bake the viewer into their ACL WHERE clause.
Add a new viewer-bound store? Follow the factory pattern, not a shared instance.

**FastMCP host/port must go through the constructor** (`build_mcp(host=..., port=...)`) — mutating
`mcp.settings.host` after construction bakes in a localhost-only DNS-rebinding allowlist and every
Cloud Run request dies with `421 Invalid Host header`.

**Retrieval is language-agnostic on purpose (§1.2).** `retrieval/hybrid.py` exposes
`hybrid_search`/`bm25_like`/`rrf` as pure functions over a `TermStore` protocol so the hot path can
later be extracted to Rust. Keep BigQuery out of these signatures.

## Invariants — do not break these

- **ACL is enforced twice (§3.3).** `retrieval/acl.py` has `visible_to`/`acl_filter` (in-process)
  AND `acl_where_clause` (SQL WHERE fragment). If you change one rule, change the other to match —
  they are intentionally redundant. (T2)
- **The ledger stores summaries only (§2.2③, T6).** `UsageRecord`, the `usage_log` DDL and the
  `log_usage` tool signature have **no verbatim-prompt field by design**. Never add a
  `prompt`/`raw_text`/`transcript` field — tests assert its absence.
- **Identity: resolved → `actor_id`, unresolved → hashed `actor_raw`** (`auth/identity.py`, §5).
  The raw OAuth `sub` must never be stored unhashed.
- **`scope_tier` personal/team/dept all reduce to "viewer is in the record's `owner_unit`"; `org`
  = everyone** (`retrieval/acl.py`, `docs/ACCESS_MATRIX.md`).
- **Owner units are never guessed.** Anything that files a record picks its owner through
  `resolve_owner_unit` (one owning unit → use it, several → the caller names one, none → refuse).
- **Promotion into a shared layer is steward-only**, gated on the candidate kind — never on whether
  a sink happens to be wired.
- **Every client-controlled `k` goes through `ServerContext._clamp_k`** (FLOOR-02). Do not raise
  `_K_CAPS` silently; the roster and `find_expert` are never break-glass.
- **Role units survive a re-sync.** Units the org sheet cannot express (steward / owner /
  export_admin) are carried forward by `sync_persons_from_sheet.manual_units` — do not remove that.
- **Onboarding is pre-registration by e-mail** (§5): `pending:<person_id>:<email>` rows in
  `oauth_identity_map` are bound to the real Google `sub` on first login, gated on
  `email_verified` and — for addresses at `[org] workspace_domains` — an `hd` claim from the
  organization's own Workspace.

## Conventions

- Python ≥3.11 (`StrEnum`, `datetime.UTC`, `zip(strict=...)`). `src/` layout, package `waca_loop`.
- The 4D-signature text in `signature/prompt.py` is **fixed Japanese prose injected verbatim**
  (§6) — don't reword or wrap it; E501 is per-file-ignored there for that reason.
- Specs are mostly Japanese; identifiers are English, comments either, with `§X` cross-references.
- AI-policy vocabulary is **level 0/1/2 (採用レベル) + 床行 (`is_floor`)**; the ledger's
  `use_category` is `l2|l1|l0|prohibited` (三色 tokens are also accepted on input and mapped
  1:1). Don't hard-code the 三色 vocabulary into new features.
- Embedding provider is `bq_ml` in production (`sql/ddl/06_embedding_model.sql`); the offline
  default is the deterministic `HashingEmbedder` so tests run without cloud.
