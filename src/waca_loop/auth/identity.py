"""Identity → Viewer resolution (§5 + §3.3).

Turns an authenticated :class:`Identity` (OAuth ``sub``) into a :class:`Viewer` carrying
the ACL inputs (org membership, units, sensitive grants). Membership is derived from the
``persons`` record's ``unit_path``; unresolved subjects become an anonymous viewer whose
ledger actor is a hashed ``actor_raw`` (§5).
"""

from __future__ import annotations

from typing import Protocol

from waca_loop.auth.oauth import Identity
from waca_loop.grounding.persons import hash_subject
from waca_loop.models import Person, Viewer


class _PersonLookup(Protocol):
    def resolve_person_id(self, subject: str) -> str | None:
        ...

    def get(self, person_id: str) -> Person | None:
        ...


def resolve_viewer(
    identity: Identity,
    persons: _PersonLookup,
    *,
    org_units: frozenset[str] | None = None,
) -> Viewer:
    """Resolve an :class:`Identity` to a :class:`Viewer` (§3.3 ACL inputs).

    - ``org_units`` is the set of unit ids considered "organization-internal"; a person
      whose ``unit_path`` intersects it is an org member. If ``None``, any resolved
      person is treated as an org member (single-org deployment, §5).
    """
    person_id = persons.resolve_person_id(identity.subject)
    if person_id is None:
        # Unmapped subject: anonymous viewer, hashed actor for the ledger (§5).
        return Viewer.anonymous(subject=hash_subject(identity.subject))

    person = persons.get(person_id)
    if person is None:
        return Viewer.anonymous(subject=hash_subject(identity.subject))

    units = frozenset(person.unit_path)
    if org_units is None:
        is_org_member = True
    else:
        is_org_member = bool(units & org_units)

    return Viewer(
        subject=identity.subject,
        person_id=person_id,
        is_org_member=is_org_member,
        is_partner=False,  # partner grants (circles) are not implemented; default false (§3.3)
        units=units,
    )


def ledger_actor(viewer: Viewer) -> tuple[str | None, str | None]:
    """Return ``(actor_id, actor_raw)`` for a ledger row (§5).

    Resolved persons populate ``actor_id``; unresolved subjects populate ``actor_raw``
    with a hash so they can be name-matched later.
    """
    if viewer.person_id:
        return viewer.person_id, None
    return None, viewer.subject  # Viewer.anonymous already stored a hashed subject


def claim_preregistration(
    identity: Identity,
    persons: object,
    workspace_domains: frozenset[str],
) -> str | None:
    """Bind a person pre-registered by email to this ``sub`` on first login (§5).

    Shared by the hosted server (``auth/as_provider.py``) and the local stdio server
    (``server.build_production_context``) so both apply the same three conditions:

    - the identity provider asserted ``email_verified`` — without it an address alone would be
      enough to claim someone else's registration;
    - if the address is at one of the organization's Workspace domains (org.toml
      ``[org] workspace_domains``), the token's ``hd`` claim must be one of those domains too.
      ``email_verified`` proves control of the mailbox today, not that the organization issued
      the account. Membership in the set (not equality with the address's own domain) because a
      multi-domain Workspace tenant may present its primary domain as ``hd``. Addresses outside
      those domains (gmail.com etc.) carry no ``hd`` and are unaffected;
    - a pending row for exactly that address still exists. The claim consumes it, so it fires
      at most once per pre-registration.

    Limit: this stops an outsider claiming a recycled organizational address, not the
    organization itself reassigning that address to someone new (that account carries a
    genuine ``hd``). The control there is roster hygiene — retire the pending row.

    Returns the bound ``person_id``, or ``None`` when nothing was (or could be) claimed.
    """
    if not identity.email or not identity.email_verified:
        return None
    from waca_loop.grounding.persons import normalize_email

    domain = normalize_email(identity.email).rpartition("@")[2]
    if domain in workspace_domains and identity.hd not in workspace_domains:
        return None
    claim = getattr(persons, "claim_pending_registration", None)
    if claim is None:
        return None
    return claim(identity.email, identity.subject)
