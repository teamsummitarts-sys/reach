"""Roles, permissions and the acting principal.

The host application has no authentication system, and REACH must not create a
second one. So this module models roles and permissions properly, enforces them
on every privileged operation, and resolves the acting principal from an
explicit request/​environment binding. When the host app gains real sessions,
:func:`current_principal` is the single place that has to change.
"""

import threading

from . import config, db
from .errors import PermissionDenied

OWNER = "OWNER"
ADMIN = "ADMIN"
CAMPAIGN_MANAGER = "CAMPAIGN_MANAGER"
REVIEWER = "REVIEWER"
ANALYST = "ANALYST"
VIEW_ONLY = "VIEW_ONLY"

ROLES = [OWNER, ADMIN, CAMPAIGN_MANAGER, REVIEWER, ANALYST, VIEW_ONLY]

# Permissions are named for the action they gate, not for the screen they sit
# behind. A model or agent can never grant itself one: the set is a constant.
PERMISSIONS = {
    "campaign.view": {OWNER, ADMIN, CAMPAIGN_MANAGER, REVIEWER, ANALYST, VIEW_ONLY},
    "campaign.create": {OWNER, ADMIN, CAMPAIGN_MANAGER},
    "campaign.run_discovery": {OWNER, ADMIN, CAMPAIGN_MANAGER},
    "campaign.cancel": {OWNER, ADMIN, CAMPAIGN_MANAGER},
    "target.edit": {OWNER, ADMIN, CAMPAIGN_MANAGER, REVIEWER},
    "draft.generate": {OWNER, ADMIN, CAMPAIGN_MANAGER},
    "outreach.approve": {OWNER, ADMIN, CAMPAIGN_MANAGER, REVIEWER},
    "outreach.send": {OWNER, ADMIN, CAMPAIGN_MANAGER},
    "suppression.edit": {OWNER, ADMIN},
    "provider.connect": {OWNER, ADMIN},
    "provider.policy_edit": {OWNER, ADMIN},
    "provider.kill_switch": {OWNER, ADMIN},
    "sender.configure": {OWNER, ADMIN},
    "spend.approve": {OWNER, ADMIN},
    "autopilot.enable": {OWNER},
    "human_task.complete": {OWNER, ADMIN, CAMPAIGN_MANAGER, REVIEWER},
    "outcome.record": {OWNER, ADMIN, CAMPAIGN_MANAGER, REVIEWER},
    "analytics.view": {OWNER, ADMIN, CAMPAIGN_MANAGER, REVIEWER, ANALYST, VIEW_ONLY},
    "relationship.view": {OWNER, ADMIN, CAMPAIGN_MANAGER, REVIEWER, ANALYST},
    "data.delete": {OWNER, ADMIN},
}

_state = threading.local()

DEFAULT_TENANT_ID = "tenant_default"
DEFAULT_PRINCIPAL_EMAIL = "owner@streetbanker.local"


def ensure_default_tenant():
    """Create the single Phase One tenant and its owner principal if absent."""
    row = db.query_one("SELECT id FROM tenant WHERE id = ?", (DEFAULT_TENANT_ID,))
    if row is None:
        db.insert("tenant", {
            "id": DEFAULT_TENANT_ID,
            "name": "Street Banker",
            "created_at": _now(),
        })
    email = config.env("REACH_PRINCIPAL_EMAIL", DEFAULT_PRINCIPAL_EMAIL)
    role = config.env("REACH_PRINCIPAL_ROLE", OWNER)
    if role not in ROLES:
        role = VIEW_ONLY
    row = db.query_one(
        "SELECT id, role FROM principal WHERE tenant_id = ? AND email = ?",
        (DEFAULT_TENANT_ID, email),
    )
    if row is None:
        principal_id = db.new_id("prin")
        db.insert("principal", {
            "id": principal_id,
            "tenant_id": DEFAULT_TENANT_ID,
            "email": email,
            "display_name": "Account Owner",
            "role": role,
            "created_at": _now(),
        })
    else:
        principal_id = row["id"]
        if row["role"] != role:
            db.update("principal", principal_id, {"role": role})
    return DEFAULT_TENANT_ID, principal_id


def _now():
    from .clock import now_iso
    return now_iso()


class Principal:
    __slots__ = ("id", "tenant_id", "email", "role", "display_name")

    def __init__(self, principal_id, tenant_id, email, role, display_name=None):
        self.id = principal_id
        self.tenant_id = tenant_id
        self.email = email
        self.role = role
        self.display_name = display_name

    def can(self, permission):
        return self.role in PERMISSIONS.get(permission, set())

    def __repr__(self):
        return f"Principal({self.email}, {self.role})"


def current_principal():
    override = getattr(_state, "principal", None)
    if override is not None:
        return override
    tenant_id, principal_id = ensure_default_tenant()
    row = db.query_one("SELECT * FROM principal WHERE id = ?", (principal_id,))
    return Principal(row["id"], tenant_id, row["email"], row["role"],
                     row["display_name"])


def set_principal(principal):
    """Bind an acting principal for this thread (request scope, or a test)."""
    _state.principal = principal


def clear_principal():
    _state.principal = None


def as_role(role):
    """Context manager returning a principal with the given role."""
    principal = current_principal()
    return _RoleScope(Principal(principal.id, principal.tenant_id, principal.email, role))


class _RoleScope:
    def __init__(self, principal):
        self.principal = principal
        self.previous = None

    def __enter__(self):
        self.previous = getattr(_state, "principal", None)
        _state.principal = self.principal
        return self.principal

    def __exit__(self, *exc):
        _state.principal = self.previous
        return False


def require(permission, principal=None):
    principal = principal or current_principal()
    if permission not in PERMISSIONS:
        raise PermissionDenied(f"Unknown permission: {permission}")
    if not principal.can(permission):
        raise PermissionDenied(
            f"Role {principal.role} may not perform {permission}"
        )
    return principal
