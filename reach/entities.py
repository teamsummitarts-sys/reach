"""Entity resolution: outlets, routes, deduplication, merge and undo.

The case that matters most: one curator running five playlists must produce one
consolidated contact target, not five near-identical emails. That is handled by
the dedup key, which is derived from the *contact route* rather than the outlet
name — because the inbox is what receives the message.

Merges are recorded, reversible, and never delete the losing row's evidence.
"""

import json

from . import audit, clock, config, db, evidence, extractor, netguard, rbac
from .errors import ValidationError

MERGE_RULE_SAME_ROUTE = "SAME_SUBMISSION_ROUTE"
MERGE_RULE_SAME_URL = "SAME_CANONICAL_URL"
MERGE_RULE_MANUAL = "MANUAL"


# --------------------------------------------------------------------------
# outlets
# --------------------------------------------------------------------------

def ensure_outlet(name, url, domain, kind, provider="open_web_fetch", external_id=None,
                  description=None, genres=None, territory=None, languages=None,
                  submissions_open=extractor.SUBMISSIONS_UNKNOWN, follower_count=None,
                  follower_count_source=None, last_activity_at=None,
                  organization_id=None, profile=None, tenant_id=None):
    """Insert or update an outlet. Identity is (provider, external_id) when the
    provider gives one, otherwise the canonical URL."""
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    now = clock.now_iso()

    row = None
    if external_id:
        row = db.query_one(
            "SELECT * FROM outlet WHERE tenant_id = ? AND provider = ? AND external_id = ?",
            (tenant_id, provider, external_id),
        )
    if row is None and url:
        row = db.query_one(
            "SELECT * FROM outlet WHERE tenant_id = ? AND url = ?", (tenant_id, url)
        )

    payload = {
        "name": name,
        "url": url,
        "domain": domain,
        "kind": kind,
        "description": description,
        "genres_json": json.dumps(genres or []),
        "territory": territory,
        "languages_json": json.dumps(languages or []),
        "submissions_open": submissions_open,
        "follower_count": follower_count,
        "follower_count_source": follower_count_source,
        "last_activity_at": last_activity_at,
        "organization_id": organization_id,
        "profile_json": json.dumps(profile or {}),
        "updated_at": now,
    }

    if row is not None:
        # Never downgrade a known submission state to UNKNOWN.
        if submissions_open == extractor.SUBMISSIONS_UNKNOWN and row["submissions_open"] != extractor.SUBMISSIONS_UNKNOWN:
            payload["submissions_open"] = row["submissions_open"]
        if follower_count is None:
            payload.pop("follower_count", None)
            payload.pop("follower_count_source", None)
        db.update("outlet", row["id"], payload)
        return row["id"]

    outlet_id = db.new_id("out")
    payload.update({
        "id": outlet_id,
        "tenant_id": tenant_id,
        "provider": provider,
        "external_id": external_id,
        "canonical_id": None,
        "created_at": now,
    })
    db.insert("outlet", payload)
    return outlet_id


def get_outlet(outlet_id):
    return db.query_one("SELECT * FROM outlet WHERE id = ?", (outlet_id,))


def canonical_outlet(outlet_id):
    """Follow a merge chain to the surviving row."""
    seen = set()
    current = get_outlet(outlet_id)
    while current is not None and current["canonical_id"] and current["id"] not in seen:
        seen.add(current["id"])
        current = get_outlet(current["canonical_id"])
    return current


# --------------------------------------------------------------------------
# submission routes
# --------------------------------------------------------------------------

def ensure_route(outlet_id, method, destination=None, provider=None, requires_login=False,
                 requires_captcha=False, cost_model=extractor.COST_UNKNOWN,
                 cost_amount=None, currency=None, instructions=None, evidence_id=None,
                 verified=False, tenant_id=None):
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    row = db.query_one(
        "SELECT * FROM submission_route WHERE outlet_id = ? AND method = ? "
        "AND destination IS ?",
        (outlet_id, method, destination),
    )
    if row is not None:
        db.update("submission_route", row["id"], {
            "requires_login": 1 if requires_login else 0,
            "requires_captcha": 1 if requires_captcha else 0,
            "cost_model": cost_model,
            "cost_amount": cost_amount,
            "currency": currency,
            "instructions": instructions,
            "evidence_id": evidence_id or row["evidence_id"],
            "verified_at": clock.now_iso() if verified else row["verified_at"],
        })
        return row["id"]

    route_id = db.new_id("route")
    db.insert("submission_route", {
        "id": route_id,
        "tenant_id": tenant_id,
        "outlet_id": outlet_id,
        "method": method,
        "destination": destination,
        "provider": provider,
        "requires_login": 1 if requires_login else 0,
        "requires_captcha": 1 if requires_captcha else 0,
        "cost_model": cost_model,
        "cost_amount": cost_amount,
        "currency": currency,
        "instructions": instructions,
        "evidence_id": evidence_id,
        "verified_at": clock.now_iso() if verified else None,
        "active": 1,
        "broken_reported_at": None,
        "created_at": clock.now_iso(),
    })
    return route_id


def routes_for_outlet(outlet_id, active_only=True):
    sql = "SELECT * FROM submission_route WHERE outlet_id = ?"
    params = [outlet_id]
    if active_only:
        sql += " AND active = 1"
    sql += " ORDER BY created_at"
    return db.query(sql, tuple(params))


def best_route(outlet_id):
    """Prefer an explicit email route, then a form, then anything else."""
    rows = routes_for_outlet(outlet_id)
    if not rows:
        return None
    order = {"EMAIL": 0, "WEB_FORM": 1, "PLATFORM_PORTAL": 2, "SOCIAL_PROFILE": 3,
             "DISTRIBUTOR": 4, "LOGIN_REQUIRED": 5, "UNSUPPORTED": 6}
    return sorted(rows, key=lambda row: (order.get(row["method"], 9),
                                         0 if row["verified_at"] else 1))[0]


def report_broken_route(route_id, reason=None):
    rbac.require("target.edit")
    db.update("submission_route", route_id, {
        "active": 0,
        "broken_reported_at": clock.now_iso(),
    })
    audit.record("route.reported_broken", entity_type="submission_route",
                 entity_id=route_id, payload={"reason": reason},
                 actor_kind=audit.ACTOR_USER)


# --------------------------------------------------------------------------
# deduplication
# --------------------------------------------------------------------------

def outlet_by_domain(domain, tenant_id=None):
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    return db.query_one(
        "SELECT * FROM outlet WHERE tenant_id = ? AND domain = ? LIMIT 1",
        (tenant_id, domain),
    )


def is_platform_domain(domain):
    """Is this a platform whose pages can never themselves be an outlet?

    Matched on the registrable domain, so open.spotify.com and music.apple.com
    resolve to their platforms. The first real discovery run qualified
    spotify.com, youtube.com, wikipedia.org and fiverr.com as "curators" —
    submission language appears on all of them, and none of them is somewhere
    a campaign can send a track.
    """
    from . import netguard

    registrable = netguard.registrable_domain(domain or "")
    return registrable in config.PLATFORM_DOMAINS


def dedup_key(outlet_id, contact_method_id=None):
    """What makes two opportunities "the same recipient".

    Contact route first (an inbox can only receive one message), then the
    registrable domain, then the outlet itself.
    """
    if contact_method_id:
        method = db.query_one("SELECT value_hash FROM contact_method WHERE id = ?",
                              (contact_method_id,))
        if method:
            return f"contact:{method['value_hash']}"
    outlet = get_outlet(outlet_id)
    if outlet is None:
        return None
    if outlet["domain"]:
        return f"domain:{netguard.registrable_domain(outlet['domain'])}"
    return f"outlet:{outlet_id}"


def canonical_target_for(campaign_id, key):
    """The one target that owns a dedup key: the earliest created.

    Resolving against the *earliest* row rather than "any other row that is not
    yet marked duplicate" is what stops a cascade — otherwise target 1 becomes a
    duplicate of target 2, which becomes a duplicate of target 3, and the
    consolidated list ends up on whichever row happened to be processed last.
    """
    if not key:
        return None
    return db.query_one(
        "SELECT * FROM campaign_target WHERE campaign_id = ? AND dedup_key = ? "
        "ORDER BY created_at, rowid LIMIT 1",
        (campaign_id, key),
    )


def find_duplicate_target(campaign_id, key, exclude_target_id=None):
    """The target ``exclude_target_id`` duplicates, or None if it is the owner."""
    canonical = canonical_target_for(campaign_id, key)
    if canonical is not None and canonical["id"] != exclude_target_id:
        return canonical
    if canonical is not None:
        return None  # this target is the owner of its key

    # A page with no route of its own still belongs to whoever already owns the
    # inbox on that domain — otherwise a curator's home page and their submission
    # page become two opportunities for the same recipient.
    if not key.startswith("domain:"):
        return None
    domain = key.split(":", 1)[1]
    sql = (
        "SELECT t.* FROM campaign_target t "
        "JOIN professional_contact pc ON pc.id = t.contact_id "
        "JOIN contact_method cm ON cm.contact_id = pc.id "
        "WHERE t.campaign_id = ? AND t.status != 'DUPLICATE' AND cm.domain IS NOT NULL"
    )
    params = [campaign_id]
    if exclude_target_id:
        sql += " AND t.id != ?"
        params.append(exclude_target_id)
    sql += " ORDER BY t.created_at, t.rowid"
    for row in db.query(sql + " LIMIT 50", tuple(params)):
        method = db.query_one(
            "SELECT domain FROM contact_method WHERE contact_id = ? AND domain IS NOT NULL "
            "LIMIT 1",
            (row["contact_id"],),
        )
        if method and netguard.registrable_domain(method["domain"]) == domain:
            return row
    return None


# --------------------------------------------------------------------------
# merge / split / undo
# --------------------------------------------------------------------------

def merge(entity_type, winner_id, loser_id, rule=MERGE_RULE_MANUAL, tenant_id=None):
    """Point ``loser`` at ``winner``. Evidence is re-parented, never deleted."""
    if winner_id == loser_id:
        raise ValidationError("Cannot merge an entity into itself")
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    table = {"outlet": "outlet", "organization": "organization",
             "professional_contact": "professional_contact"}.get(entity_type)
    if table is None:
        raise ValidationError(f"Cannot merge entity type {entity_type}")

    db.update(table, loser_id, {"canonical_id": winner_id})
    db.execute(
        "UPDATE evidence_packet SET entity_id = ? WHERE entity_type = ? AND entity_id = ?",
        (winner_id, entity_type, loser_id),
    )

    merge_id = db.new_id("merge")
    db.insert("entity_merge", {
        "id": merge_id,
        "tenant_id": tenant_id,
        "entity_type": entity_type,
        "winner_id": winner_id,
        "loser_id": loser_id,
        "rule": rule,
        "merged_at": clock.now_iso(),
        "undone_at": None,
    })
    audit.record("entity.merged", entity_type=entity_type, entity_id=winner_id,
                 payload={"loser": loser_id, "rule": rule})
    return merge_id


def undo_merge(merge_id):
    row = db.query_one("SELECT * FROM entity_merge WHERE id = ?", (merge_id,))
    if row is None or row["undone_at"]:
        return False
    table = row["entity_type"]
    db.update(table, row["loser_id"], {"canonical_id": None})
    db.update("entity_merge", merge_id, {"undone_at": clock.now_iso()})
    audit.record("entity.merge_undone", entity_type=row["entity_type"],
                 entity_id=row["winner_id"], payload={"merge_id": merge_id})
    return True


def merges(tenant_id=None, limit=50):
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    return db.query(
        "SELECT * FROM entity_merge WHERE tenant_id = ? ORDER BY merged_at DESC LIMIT ?",
        (tenant_id, limit),
    )


def auto_merge_by_route(tenant_id=None):
    """Deterministic merge: outlets on the same domain sharing one email route.

    Runs without human approval because the rule is exact — same registrable
    domain, same destination address. Anything fuzzier is only ever a suggestion.
    """
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    rows = db.query(
        "SELECT r.destination, r.outlet_id, o.domain FROM submission_route r "
        "JOIN outlet o ON o.id = r.outlet_id "
        "WHERE r.tenant_id = ? AND r.method = 'EMAIL' AND r.active = 1 "
        "AND o.canonical_id IS NULL",
        (tenant_id,),
    )
    groups = {}
    for row in rows:
        if not row["destination"]:
            continue
        key = (netguard.registrable_domain(row["domain"] or ""), row["destination"])
        groups.setdefault(key, []).append(row["outlet_id"])

    merged = []
    for key, outlet_ids in groups.items():
        if len(outlet_ids) < 2:
            continue
        ordered = sorted(set(outlet_ids))
        winner = ordered[0]
        for loser in ordered[1:]:
            merged.append(merge("outlet", winner, loser, MERGE_RULE_SAME_ROUTE, tenant_id))
    return merged


def suggest_merges(tenant_id=None, limit=20):
    """Fuzzy candidates for a human to confirm. Never applied automatically."""
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    rows = db.query(
        "SELECT id, name, domain, kind FROM outlet WHERE tenant_id = ? AND canonical_id IS NULL",
        (tenant_id,),
    )
    by_name = {}
    for row in rows:
        key = "".join(char for char in (row["name"] or "").lower() if char.isalnum())
        by_name.setdefault(key, []).append(row)
    suggestions = []
    for key, group in by_name.items():
        if len(group) < 2 or not key:
            continue
        suggestions.append({
            "reason": "Identical normalized outlet name",
            "candidates": [dict(item) for item in group],
        })
    return suggestions[:limit]


def outlet_evidence(outlet_id):
    return evidence.summary("outlet", outlet_id)
