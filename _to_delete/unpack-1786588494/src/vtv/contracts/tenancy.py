"""Stages 25 and 29 — who is asking, and what they are allowed to do.

Multi-tenancy is not a feature that can be retrofitted. Every query either
carries an organisation id or it is a data leak waiting for the first customer
who notices. So it lives here, in contracts, alongside the documents it scopes.

Three ideas carry the whole model.

**The organisation is the tenant.** Not the user. A user may belong to several
organisations, and every piece of content belongs to exactly one. The identifier
that matters on a query is `organisation_id`, and `docs/SECURITY.md` states the
rule the repositories enforce: no read without it.

**Roles are coarse; capabilities are fine.** Roles exist because humans reason
in them and administrators assign them. Capabilities exist because code should
ask "may this principal delete a project", never "is this principal an admin" —
the latter spreads policy through the codebase and is how privilege escalation
bugs are written.

**An API key's secret is never stored.** Only a hash. A database dump does not
hand over the keys, and there is exactly one moment the plaintext exists: the
response to the call that created it.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import Field, model_validator

from vtv.contracts.base import (
    Id,
    IdPrefix,
    RootDocument,
    Timestamped,
    VTVModel,
    new_id,
    utc_now,
)


class Role(str, Enum):
    """What a member may do, in the vocabulary administrators think in.

    Ordered by privilege so comparisons are possible, but code should ask for a
    permission rather than compare roles — see :func:`capabilities_for`.
    """

    #: Read-only. For auditors, and for the "share a link with my manager" case.
    VIEWER = "viewer"
    #: Creates and edits their own projects. The default for a new member.
    EDITOR = "editor"
    #: Everything in the organisation's content, plus member management.
    ADMIN = "admin"
    #: Billing, deletion of the organisation, and transferring ownership.
    OWNER = "owner"
    #: Machine principals. Deliberately not a human role: a key issued for CI
    #: should never be able to add members, whatever its holder's own role is.
    SERVICE = "service"


class Capability(str, Enum):
    """The atomic questions code is allowed to ask.

    Named for the action rather than the resource shape, because that is how
    they are checked at the call site: ``require(principal, Capability.
    PROJECT_DELETE)``.
    """

    PROJECT_READ = "project:read"
    PROJECT_CREATE = "project:create"
    PROJECT_UPDATE = "project:update"
    PROJECT_DELETE = "project:delete"
    #: Spending money. Separated from create because a viewer-plus-render role
    #: is a real thing customers ask for and a real thing to get wrong.
    RENDER_SUBMIT = "render:submit"
    MEMBER_READ = "member:read"
    MEMBER_MANAGE = "member:manage"
    API_KEY_MANAGE = "apikey:manage"
    BILLING_READ = "billing:read"
    BILLING_MANAGE = "billing:manage"
    AUDIT_READ = "audit:read"
    ORGANISATION_MANAGE = "organisation:manage"


#: Role to capabilities. Explicit rather than computed from an ordering: a table
#: somebody can read line by line during a security review is worth more than a
#: clever derivation, and every escalation bug hides in the clever version.
ROLE_CAPABILITIES: dict[Role, frozenset[Capability]] = {
    Role.VIEWER: frozenset(
        {
            Capability.PROJECT_READ,
            Capability.MEMBER_READ,
        }
    ),
    Role.EDITOR: frozenset(
        {
            Capability.PROJECT_READ,
            Capability.PROJECT_CREATE,
            Capability.PROJECT_UPDATE,
            Capability.RENDER_SUBMIT,
            Capability.MEMBER_READ,
        }
    ),
    Role.ADMIN: frozenset(
        {
            Capability.PROJECT_READ,
            Capability.PROJECT_CREATE,
            Capability.PROJECT_UPDATE,
            Capability.PROJECT_DELETE,
            Capability.RENDER_SUBMIT,
            Capability.MEMBER_READ,
            Capability.MEMBER_MANAGE,
            Capability.API_KEY_MANAGE,
            Capability.BILLING_READ,
            Capability.AUDIT_READ,
        }
    ),
    Role.OWNER: frozenset(Capability),
    Role.SERVICE: frozenset(
        {
            Capability.PROJECT_READ,
            Capability.PROJECT_CREATE,
            Capability.PROJECT_UPDATE,
            Capability.RENDER_SUBMIT,
        }
    ),
}


def capabilities_for(role: Role) -> frozenset[Capability]:
    """The capabilities a role grants. Unknown roles grant nothing."""
    return ROLE_CAPABILITIES.get(role, frozenset())


class PlanTier(str, Enum):
    """Commercial tier. Drives quotas and rate limits, never features in code.

    Feature flags read from the plan record; branching on the tier name inside
    business logic is how a pricing change becomes a code change.
    """

    FREE = "free"
    STARTER = "starter"
    PROFESSIONAL = "professional"
    BUSINESS = "business"
    ENTERPRISE = "enterprise"


class DataResidency(str, Enum):
    """Where this tenant's content may be processed.

    A commitment enterprise customers buy and auditors verify. Enforced at
    provider selection: a provider whose region does not match is not a
    candidate, exactly as an unverified data policy excludes one from the voice
    path.
    """

    ANY = "any"
    EU = "eu"
    US = "us"
    INDIA = "india"


class Organisation(RootDocument, Timestamped):
    """The tenant. Every piece of content in the system belongs to exactly one."""

    document_name = "organisation"

    organisation_id: Id = Field(default_factory=lambda: new_id(IdPrefix.PROJECT))
    name: str = Field(min_length=1, max_length=200)
    #: URL-safe handle, unique across the deployment.
    slug: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")
    plan: PlanTier = PlanTier.FREE
    residency: DataResidency = DataResidency.ANY

    #: Consent travels with the tenant as well as with the project, so a
    #: contract negotiated at the organisation level cannot be undone by a
    #: default on one project.
    allow_product_improvement: bool = False
    allow_human_review: bool = False

    #: Days before content is deleted. `None` means the per-project default.
    retention_days: int | None = Field(default=None, ge=1, le=3650)
    suspended: bool = False
    suspended_reason: str | None = Field(default=None, max_length=200)
    deleted_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        return not self.suspended and self.deleted_at is None

    @property
    def trains_on_content(self) -> bool:
        """Whether this tenant's content may be used to improve the product.

        Deliberately a positive question with a negative default, so the
        training-data export filters on the same field the customer set rather
        than on the absence of an objection.
        """
        return self.allow_product_improvement


class User(RootDocument, Timestamped):
    """A human principal. May belong to several organisations."""

    document_name = "user"

    user_id: Id = Field(default_factory=lambda: new_id(IdPrefix.PROJECT))
    email: str = Field(min_length=3, max_length=320)
    display_name: str | None = Field(default=None, max_length=200)
    #: Argon2/bcrypt digest when password login is used, absent for SSO-only
    #: accounts. Never a plaintext or reversibly-encrypted password.
    password_hash: str | None = Field(default=None, max_length=512)
    #: Identity provider subject, for SSO. Stage 29.
    sso_subject: str | None = Field(default=None, max_length=200)
    mfa_enabled: bool = False
    last_seen_at: datetime | None = None
    disabled: bool = False

    @model_validator(mode="after")
    def _has_some_credential(self) -> User:
        # A user with neither is unreachable, which is a data-integrity bug that
        # presents later as a mysterious login failure.
        if self.password_hash is None and self.sso_subject is None:
            raise ValueError("a user needs either a password hash or an SSO subject")
        return self

    @property
    def email_domain(self) -> str:
        _, _, domain = self.email.rpartition("@")
        return domain.lower()


class Membership(VTVModel):
    """A user's role within one organisation."""

    user_id: Id
    organisation_id: Id
    role: Role = Role.EDITOR
    invited_by: Id | None = None
    accepted_at: datetime | None = None

    @property
    def capabilities(self) -> frozenset[Capability]:
        return capabilities_for(self.role)


class ApiKey(RootDocument, Timestamped):
    """A machine credential. The secret is never stored — only its digest.

    The prefix is stored in the clear so a key can be identified in a log, in an
    audit entry and in the UI list without the secret being recoverable. That is
    the difference between "revoke the key ending in 4f2a" being a usable
    instruction and being a guess.
    """

    document_name = "api_key"

    api_key_id: Id = Field(default_factory=lambda: new_id(IdPrefix.PROJECT))
    organisation_id: Id
    name: str = Field(min_length=1, max_length=120)
    #: Public identifying prefix, e.g. ``vtv_live_7f3a``.
    prefix: str = Field(min_length=4, max_length=32)
    #: Hex SHA-256 of the full secret. Comparison is constant-time.
    secret_hash: str = Field(min_length=64, max_length=128)
    role: Role = Role.SERVICE
    #: Narrower than the role when set: the intersection is what applies. A key
    #: for a metrics dashboard should carry `project:read` and nothing else even
    #: if its role would allow more.
    scopes: list[Capability] = Field(default_factory=list, max_length=32)
    created_by: Id | None = None
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        if self.revoked_at is not None:
            return False
        return self.expires_at is None or self.expires_at > utc_now()

    @property
    def capabilities(self) -> frozenset[Capability]:
        """Role capabilities, narrowed by scopes when scopes are present."""
        granted = capabilities_for(self.role)
        if not self.scopes:
            return granted
        return granted & frozenset(self.scopes)


class PrincipalKind(str, Enum):
    USER = "user"
    API_KEY = "api_key"
    #: Internal jobs. Never reachable from an HTTP request.
    SYSTEM = "system"
    #: Nobody authenticated. Carries no capabilities at all, and exists so that
    #: unauthenticated code paths hold a principal rather than a `None` that
    #: someone will forget to check.
    ANONYMOUS = "anonymous"


class Principal(VTVModel):
    """Who is making this request, resolved once at the edge and passed down.

    Nothing below the API re-authenticates. Everything below receives this and
    asks it questions. That is what makes authorisation testable without an
    HTTP client and what stops a second, subtly different check appearing in a
    service three layers down.
    """

    kind: PrincipalKind = PrincipalKind.ANONYMOUS
    #: User id, api key id, or a service name.
    subject: str = Field(default="anonymous", max_length=128)
    organisation_id: Id | None = None
    role: Role | None = None
    granted: list[Capability] = Field(default_factory=list, max_length=32)
    #: Correlates every log line, audit entry and event for one request.
    request_id: str | None = Field(default=None, max_length=64)

    @property
    def is_authenticated(self) -> bool:
        return self.kind is not PrincipalKind.ANONYMOUS

    @property
    def capabilities(self) -> frozenset[Capability]:
        if self.kind is PrincipalKind.SYSTEM:
            return frozenset(Capability)
        return frozenset(self.granted)

    def can(self, capability: Capability) -> bool:
        """Whether this principal holds a capability. Fails closed.

        A request-derived principal with no organisation can do nothing at all,
        which is the safe answer for a half-resolved authentication. `SYSTEM` is
        the one exception, because it is never constructed from a request and
        some internal work (retention sweeps, migrations) is genuinely
        cross-tenant; tenant scoping for it is enforced by `owns`, which stays
        strict.
        """
        if self.kind is PrincipalKind.SYSTEM:
            return True
        if self.kind is PrincipalKind.ANONYMOUS or self.organisation_id is None:
            return False
        return capability in self.capabilities

    def owns(self, organisation_id: str | None) -> bool:
        """Whether this principal may touch content in that organisation.

        The single most important check in a multi-tenant system, and the reason
        it is one function: a bug here is a cross-customer data leak, and one
        function is auditable in a way that thirty inline comparisons are not.
        """
        if organisation_id is None or self.organisation_id is None:
            return False
        return self.organisation_id == organisation_id

    @classmethod
    def system(cls, name: str, organisation_id: str | None = None) -> Principal:
        """An internal principal for background work. Never from a request."""
        return cls(
            kind=PrincipalKind.SYSTEM,
            subject=name[:128],
            organisation_id=organisation_id,
            role=Role.OWNER,
        )

    @classmethod
    def anonymous(cls, request_id: str | None = None) -> Principal:
        return cls(request_id=request_id)


class AuditAction(str, Enum):
    """What happened. A closed vocabulary, so the log is queryable."""

    LOGIN_SUCCEEDED = "auth.login.succeeded"
    LOGIN_FAILED = "auth.login.failed"
    KEY_CREATED = "apikey.created"
    KEY_REVOKED = "apikey.revoked"
    KEY_USED = "apikey.used"
    MEMBER_ADDED = "member.added"
    MEMBER_ROLE_CHANGED = "member.role_changed"
    MEMBER_REMOVED = "member.removed"
    PROJECT_CREATED = "project.created"
    PROJECT_DELETED = "project.deleted"
    PROJECT_EXPORTED = "project.exported"
    DOCUMENT_UPLOADED = "document.uploaded"
    RENDER_SUBMITTED = "render.submitted"
    DATA_DELETED = "data.deleted"
    DATA_EXPORTED = "data.exported"
    SETTINGS_CHANGED = "settings.changed"
    PERMISSION_DENIED = "auth.permission_denied"
    RATE_LIMITED = "auth.rate_limited"
    UPLOAD_REJECTED = "upload.rejected"


class AuditEvent(RootDocument, Timestamped):
    """One entry in the append-only audit log.

    Append-only is the whole point: an actor who can edit the record of what
    they did has not been audited. The storage layer enforces it; this contract
    carries no mutation methods and every field is set once at construction.
    """

    document_name = "audit_event"

    audit_event_id: Id = Field(default_factory=lambda: new_id(IdPrefix.PROJECT))
    organisation_id: Id | None = None
    action: AuditAction
    #: Who did it, as ``kind:subject`` so it survives the user being deleted.
    actor: str = Field(min_length=1, max_length=160)
    actor_role: Role | None = None
    #: What it was done to, e.g. ``project:prj_...``.
    target: str | None = Field(default=None, max_length=200)
    #: Whether the action succeeded. Denied attempts are the entries that
    #: matter most during an incident, so they are recorded, not filtered.
    succeeded: bool = True
    #: Truncated and redacted by the writer; never raw request bodies.
    detail: dict[str, str] = Field(default_factory=dict)
    request_id: str | None = Field(default=None, max_length=64)
    #: Stored for security investigation. Subject to the same retention as the
    #: rest of the audit log, and never joined to content.
    ip_address: str | None = Field(default=None, max_length=64)
    user_agent: str | None = Field(default=None, max_length=256)

    @property
    def is_security_relevant(self) -> bool:
        return not self.succeeded or self.action in {
            AuditAction.LOGIN_FAILED,
            AuditAction.PERMISSION_DENIED,
            AuditAction.RATE_LIMITED,
            AuditAction.KEY_CREATED,
            AuditAction.KEY_REVOKED,
            AuditAction.UPLOAD_REJECTED,
        }


__all__ = [
    "ROLE_CAPABILITIES",
    "ApiKey",
    "AuditAction",
    "AuditEvent",
    "Capability",
    "DataResidency",
    "Membership",
    "Organisation",
    "PlanTier",
    "Principal",
    "PrincipalKind",
    "Role",
    "User",
    "capabilities_for",
]
