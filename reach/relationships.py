"""Relationship intelligence.

A durable record per curator, outlet and organization: what was sent, what came
back, how long they took, what they prefer, and when it is acceptable to write
again. Notes are private to the tenant that wrote them.
"""

from . import audit, clock, db, rbac

DEFAULT_RECONTACT_DAYS = 60


def touch(contact_id, outlet_id, last_contact_at=None, tenant_id=None):
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    row = db.query_one(
        "SELECT * FROM relationship WHERE tenant_id = ? AND contact_id IS ? AND outlet_id IS ?",
        (tenant_id, contact_id, outlet_id),
    )
    now = clock.now_iso()
    last_contact_at = last_contact_at or now
    if row is None:
        relationship_id = db.new_id("rel")
        db.insert("relationship", {
            "id": relationship_id,
            "tenant_id": tenant_id,
            "contact_id": contact_id,
            "outlet_id": outlet_id,
            "owner_principal_id": rbac.current_principal().id,
            "preferred_method": "EMAIL",
            "last_contact_at": last_contact_at,
            "next_eligible_at": clock.days_from_now(DEFAULT_RECONTACT_DAYS),
            "accepted_count": 0,
            "declined_count": 0,
            "placement_count": 0,
            "created_at": now,
            "updated_at": now,
        })
        return relationship_id
    db.update("relationship", row["id"], {
        "last_contact_at": last_contact_at,
        "next_eligible_at": clock.days_from_now(DEFAULT_RECONTACT_DAYS),
        "updated_at": now,
    })
    return row["id"]


def get(contact_id, outlet_id, tenant_id=None):
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    return db.query_one(
        "SELECT * FROM relationship WHERE tenant_id = ? AND contact_id IS ? AND outlet_id IS ?",
        (tenant_id, contact_id, outlet_id),
    )


def record_outcome(contact_id, outlet_id, accepted=False, declined=False, placed=False,
                   tenant_id=None):
    relationship_id = touch(contact_id, outlet_id, tenant_id=tenant_id)
    row = db.query_one("SELECT * FROM relationship WHERE id = ?", (relationship_id,))
    db.update("relationship", relationship_id, {
        "accepted_count": row["accepted_count"] + (1 if accepted else 0),
        "declined_count": row["declined_count"] + (1 if declined else 0),
        "placement_count": row["placement_count"] + (1 if placed else 0),
        "updated_at": clock.now_iso(),
    })
    return relationship_id


def block_recontact(contact_id, outlet_id, days=3650, tenant_id=None):
    """After a decline or an opt-out, the next eligible date moves far out."""
    relationship_id = touch(contact_id, outlet_id, tenant_id=tenant_id)
    db.update("relationship", relationship_id, {
        "next_eligible_at": clock.days_from_now(days),
        "updated_at": clock.now_iso(),
    })
    return relationship_id


def may_contact(contact_id, outlet_id, tenant_id=None):
    row = get(contact_id, outlet_id, tenant_id)
    if row is None:
        return True, None
    if row["next_eligible_at"] and not clock.is_past(row["next_eligible_at"]):
        return False, row["next_eligible_at"]
    return True, None


def add_note(relationship_id, body, tenant_id=None):
    """Private to this tenant. Never visible to another artist's account."""
    principal = rbac.current_principal()
    tenant_id = tenant_id or principal.tenant_id
    note_id = db.new_id("note")
    db.insert("relationship_note", {
        "id": note_id,
        "tenant_id": tenant_id,
        "relationship_id": relationship_id,
        "body": body,
        "author_id": principal.id,
        "created_at": clock.now_iso(),
    })
    audit.record("relationship.note_added", entity_type="relationship",
                 entity_id=relationship_id, actor_kind=audit.ACTOR_USER)
    return note_id


def notes(relationship_id, tenant_id=None):
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    return db.query(
        "SELECT * FROM relationship_note WHERE relationship_id = ? AND tenant_id = ? "
        "ORDER BY created_at DESC",
        (relationship_id, tenant_id),
    )


def listing(tenant_id=None, limit=200):
    """The Relationships screen. Scoped to one tenant by the query itself."""
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    return db.query(
        "SELECT r.*, o.name AS outlet_name, o.kind AS outlet_kind, o.url AS outlet_url, "
        "(SELECT COUNT(*) FROM submission s JOIN campaign_target t ON t.id = s.target_id "
        " WHERE t.outlet_id = r.outlet_id AND s.tenant_id = r.tenant_id) AS submissions, "
        "(SELECT COUNT(*) FROM response rs JOIN campaign_target t ON t.id = rs.target_id "
        " WHERE t.outlet_id = r.outlet_id AND rs.tenant_id = r.tenant_id) AS responses, "
        "(SELECT COUNT(*) FROM relationship_note n WHERE n.relationship_id = r.id "
        " AND n.tenant_id = r.tenant_id) AS note_count, "
        "(SELECT COUNT(*) FROM response rs JOIN campaign_target t ON t.id = rs.target_id "
        " WHERE t.outlet_id = r.outlet_id AND rs.tenant_id = r.tenant_id "
        " AND rs.kind = 'ACCEPT') AS accepted_responses, "
        "(SELECT COUNT(*) FROM campaign_target t WHERE t.outlet_id = r.outlet_id "
        " AND t.tenant_id = r.tenant_id AND t.status = 'OPTED_OUT') AS opted_out "
        "FROM relationship r LEFT JOIN outlet o ON o.id = r.outlet_id "
        "WHERE r.tenant_id = ? ORDER BY r.updated_at DESC LIMIT ?",
        (tenant_id, limit),
    )


def history(outlet_id, tenant_id=None):
    """Everything this tenant has done with one outlet."""
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    return {
        "submissions": db.query(
            "SELECT s.* FROM submission s JOIN campaign_target t ON t.id = s.target_id "
            "WHERE t.outlet_id = ? AND s.tenant_id = ? ORDER BY s.created_at DESC",
            (outlet_id, tenant_id),
        ),
        "responses": db.query(
            "SELECT r.* FROM response r JOIN campaign_target t ON t.id = r.target_id "
            "WHERE t.outlet_id = ? AND r.tenant_id = ? ORDER BY r.received_at DESC",
            (outlet_id, tenant_id),
        ),
        "placements": db.query(
            "SELECT * FROM placement WHERE outlet_id = ? AND tenant_id = ? "
            "ORDER BY created_at DESC",
            (outlet_id, tenant_id),
        ),
    }


def response_time_stats(outlet_id, tenant_id=None):
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    rows = db.query(
        "SELECT s.sent_at, r.received_at FROM response r "
        "JOIN submission s ON s.id = r.submission_id "
        "JOIN campaign_target t ON t.id = r.target_id "
        "WHERE t.outlet_id = ? AND r.tenant_id = ? AND s.sent_at IS NOT NULL",
        (outlet_id, tenant_id),
    )
    deltas = []
    for row in rows:
        sent = clock.parse(row["sent_at"])
        received = clock.parse(row["received_at"])
        if sent and received:
            deltas.append((received - sent).total_seconds() / 86400.0)
    if not deltas:
        return {"count": 0, "average_days": None}
    return {"count": len(deltas), "average_days": round(sum(deltas) / len(deltas), 1)}
