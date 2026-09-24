"""Authorisation: one place, one function, one answer.

Every access decision in the system goes through :func:`require`. That is not
tidiness — it is the only way authorisation stays reviewable. Thirty inline
``if principal.role == "admin"`` comparisons cannot be audited; one function
with one test file can.

Two checks, always both, always in this order:

1. **Tenant.** Does this principal belong to the organisation that owns the
   thing? A capability check that skips this is how one customer reads another
   customer's projects — the request is perfectly authorised, just against the
   wrong tenant.
2. **Capability.** Does this principal hold the specific right being exercised?

Denials are recorded. During an incident the entries that matter most are the
attempts that failed, so they are audited rather than filtered out.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from vtv.contracts.errors import ErrorCode, PolicyViolation
from vtv.contracts.tenancy import (
    AuditAction,
    Capability,
    Organisation,
    Principal,
    PrincipalKind,
)


class NotAuthenticated(PolicyViolation):
    """No usable credential was presented."""

    code = ErrorCode.NOT_AUTHENTICATED
    user_message = "Please sign in to continue."


class Forbidden(PolicyViolation):
    """A valid principal that may not do this."""

    code = ErrorCode.PERMISSION_DENIED
    user_message = "You do not have permission to do that."


def require(
    principal: Principal,
    capability: Capability,
    *,
    organisation_id: str | None = None,
    resource: str | None = None,
) -> None:
    """Raise unless ``principal`` may exercise ``capability`` here.

    ``organisation_id`` is the tenant that owns the resource being touched. It
    is optional only for organisation-less actions such as creating the first
    organisation; whenever a resource exists, passing it is mandatory, and the
    call site that forgets is the bug this signature is shaped to make obvious.
    """
    if not principal.is_authenticated:
        raise NotAuthenticated(
            f"anonymous request attempted {capability.value}"
            + (f" on {resource}" if resource else "")
        )

    if organisation_id is not None and not principal.owns(organisation_id):
        # Deliberately the same error and the same message as a missing
        # capability. Telling an attacker "that project exists but is not yours"
        # confirms the id, which is a disclosure in itself.
        raise Forbidden(
            f"{principal.kind.value}:{principal.subject} is not a member of "
            f"{organisation_id}"
        )

    if not principal.can(capability):
        raise Forbidden(
            f"{principal.kind.value}:{principal.subject} lacks {capability.value}"
        )


def may(
    principal: Principal,
    capability: Capability,
    *,
    organisation_id: str | None = None,
) -> bool:
    """The non-raising form, for deciding what to *show* rather than allow.

    Used to hide a button the user cannot press. Never used in place of
    :func:`require` at the point the action happens: a UI that hides a control
    and an API that permits it is a UI-only security model.
    """
    try:
        require(principal, capability, organisation_id=organisation_id)
    except PolicyViolation:
        return False
    return True


@dataclass
class Authorizer:
    """Authorisation with an audit trail attached.

    Wraps :func:`require` so every denial is recorded without each call site
    having to remember to record it. The sink is a callable rather than the
    audit log itself so this module stays free of storage concerns.
    """

    record: object = None  # AuditLog | None
    #: Asked whether one organisation is suspended. A callable rather than a
    #: set, because holding the set meant recomputing it on every request —
    #: which was a full scan and JSON parse of every tenant in the deployment.
    #: Checked before capabilities: a suspended tenant's admin is still an
    #: admin, and still must not be able to spend money.
    is_suspended: Callable[[str], bool] | None = None

    def check(
        self,
        principal: Principal,
        capability: Capability,
        *,
        organisation_id: str | None = None,
        resource: str | None = None,
    ) -> None:
        if (
            organisation_id is not None
            and self.is_suspended is not None
            and self.is_suspended(organisation_id)
        ):
            self._deny(principal, capability, organisation_id, resource, "suspended")
            raise Forbidden(f"organisation {organisation_id} is suspended")
        try:
            require(
                principal,
                capability,
                organisation_id=organisation_id,
                resource=resource,
            )
        except PolicyViolation:
            self._deny(principal, capability, organisation_id, resource, "denied")
            raise

    def _deny(
        self,
        principal: Principal,
        capability: Capability,
        organisation_id: str | None,
        resource: str | None,
        reason: str,
    ) -> None:
        if self.record is None:
            return
        self.record.write(  # type: ignore[attr-defined]
            action=AuditAction.PERMISSION_DENIED,
            principal=principal,
            organisation_id=organisation_id or principal.organisation_id,
            target=resource,
            succeeded=False,
            detail={"capability": capability.value, "reason": reason},
        )


def tenant_of(organisation: Organisation) -> str:
    """The tenant key for an organisation, refusing a deleted one.

    A deleted organisation's content must stop being reachable immediately,
    including through a credential that was valid a moment ago.
    """
    if not organisation.is_active:
        raise Forbidden("this organisation is no longer active")
    return organisation.organisation_id


def principal_for_key(
    *, api_key_id: str, organisation_id: str, role: object, capabilities: object
) -> Principal:
    """Build the principal a verified API key stands for."""
    from vtv.contracts.tenancy import Role

    return Principal(
        kind=PrincipalKind.API_KEY,
        subject=api_key_id,
        organisation_id=organisation_id,
        role=role if isinstance(role, Role) else Role.SERVICE,
        granted=sorted(capabilities, key=lambda c: c.value)  # type: ignore[call-overload]
        if capabilities
        else [],
    )


__all__ = [
    "Authorizer",
    "Forbidden",
    "NotAuthenticated",
    "may",
    "principal_for_key",
    "require",
    "tenant_of",
]
