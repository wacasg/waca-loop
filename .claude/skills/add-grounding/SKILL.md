---
name: add-grounding
description: >-
  Add or update an organization terms grounding record (term or person) correctly — mandatory scope tags,
  status, owner_unit, and embedding backfill. Use when the user wants to add/insert a term, social
  jargon, abbreviation, product name, or a person into the grounding layer.
---

# add-grounding — add a term or person to the terms table

Grounding records feed ACL-filtered retrieval (§3.3), so they MUST carry correct scope tags or
they will either leak or be invisible. Follow this checklist (§2.1/§2.2).

## A term (`<DS_GROUNDING>.terms`, §2.2 ①)
Required: `term_id` (UUID), `term`, `definition`, and the **three scope tags**:
- `scope_tier`: `personal` | `team` | `dept` | `org`
- `scope_audience`: `internal` | `partner` | `public`
- `scope_sensitivity`: `normal` | `sensitive`
- `owner_unit`: REQUIRED for any non-`org` tier and for every `sensitive` record — it's the unit
  the viewer must belong to (see `retrieval/acl.py:visible_to`). Omitting it makes the record
  invisible to everyone (fails tier/sensitivity gates).
- `status`: `approved` (searchable) | `draft` (excluded — `WHERE status='approved'`).
- `version` starts at 1; `created_at`/`updated_at` = now.
- Note: a term's *existence* can itself be confidential (internal code names) — mark those
  `sensitive` + the owning `owner_unit`.

After inserting, run `python scripts/backfill_embeddings.py` so `embedding` is populated (empty embedding ⇒ the
term never appears in semantic results).

## A person (`<DS_GROUNDING>.persons`, §2.2 ②) — PII, separate table
Required: `person_id` (UUID, distinct from OAuth `sub`), `display_name`, `unit_path[]`,
`consent_internal` (default true — opt-out), `consent_external` (default false — opt-in),
`status` (`active` | `frozen`). To let `lookup_terms` resolve a person, `consent_internal` must
be true and the viewer must be an org member. Map their OAuth `sub` in `oauth_identity_map` (§5).

## Verify
- In-process check: `python -c "from waca_loop.retrieval.acl import visible_to; ..."` against
  a representative `Viewer` to confirm the new record is visible to the intended audience only.
- Don't hand-write embeddings — let `scripts/backfill_embeddings.py` compute them.
