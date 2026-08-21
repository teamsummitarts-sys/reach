"""Campaign analytics.

Every number here is a count or an aggregate of a persisted row. Where REACH
lacks the data to answer, the metric reports UNKNOWN rather than zero — a zero
that means "not measured" is the most dangerous number a dashboard can show.

Attribution is labelled: observed correlation, modelled contribution, directly
attributable, or unknown. REACH does not claim a placement caused activity it
cannot trace.
"""

from . import campaigns, catalog, clock, db, evidence, jobs, outcomes, rbac, firstparty

OBSERVED_CORRELATION = "OBSERVED_CORRELATION"
MODELLED_CONTRIBUTION = "MODELLED_CONTRIBUTION"
DIRECTLY_ATTRIBUTABLE = "DIRECTLY_ATTRIBUTABLE"
UNKNOWN_ATTRIBUTION = "UNKNOWN"

BASELINE = "BASELINE"
OUTCOME = "OUTCOME"


def capture_baseline(campaign_id, tenant_id=None):
    """Snapshot first-party performance before outreach starts."""
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    campaign = campaigns.get(campaign_id)
    if campaign is None:
        return []
    recording = catalog.get_recording(campaign["recording_id"])
    baseline = firstparty.performance_baseline(catalog.track(recording))

    captured = []
    # Only figures the rights holder actually entered are snapshotted. An
    # absent one is skipped, not stored as a zero that would later read as a
    # measured baseline of nothing.
    for metric, value in [("streams", baseline.get("streams"))]:
        if value is None:
            continue
        snapshot_id = db.new_id("snap")
        db.insert("analytics_snapshot", {
            "id": snapshot_id,
            "tenant_id": tenant_id,
            "campaign_id": campaign_id,
            "recording_id": campaign["recording_id"],
            "placement_id": None,
            "kind": BASELINE,
            "metric": metric,
            "value": float(value),
            "source": "reach_catalog",
            "attribution": UNKNOWN_ATTRIBUTION,
            "captured_at": clock.now_iso(),
        })
        captured.append(snapshot_id)
    return captured


def snapshots(campaign_id, kind=None, tenant_id=None):
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    sql = "SELECT * FROM analytics_snapshot WHERE tenant_id = ? AND campaign_id = ?"
    params = [tenant_id, campaign_id]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    sql += " ORDER BY captured_at"
    return db.query(sql, tuple(params))


def baseline_for(campaign_id, metric, tenant_id=None):
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    row = db.query_one(
        "SELECT * FROM analytics_snapshot WHERE tenant_id = ? AND campaign_id = ? "
        "AND kind = ? AND metric = ? ORDER BY captured_at LIMIT ?",
        (tenant_id, campaign_id, BASELINE, metric, 1),
    )
    return row


def campaign_metrics(campaign_id):
    """The Overview numbers. Each one is a query, not a constant."""
    counts = campaigns.status_counts(campaign_id)
    total = sum(counts.values())

    high_fit = db.query_one(
        "SELECT COUNT(*) AS n FROM campaign_target t WHERE t.campaign_id = ? AND ("
        " SELECT score FROM opportunity_score s WHERE s.target_id = t.id "
        " ORDER BY created_at DESC LIMIT 1) >= 70",
        (campaign_id,),
    )["n"]

    verified = db.query_one(
        "SELECT COUNT(DISTINCT t.id) AS n FROM campaign_target t "
        "JOIN professional_contact pc ON pc.id = t.contact_id "
        "JOIN contact_method cm ON cm.contact_id = pc.id "
        "WHERE t.campaign_id = ? AND cm.verified_at IS NOT NULL "
        "AND cm.category IN ('EXPLICIT_SUBMISSION_ROUTE','VERIFIED_PUBLIC_BUSINESS',"
        "'ROLE_BASED_ADDRESS')",
        (campaign_id,),
    )["n"]

    submitted = db.query_one(
        "SELECT COUNT(*) AS n FROM submission s JOIN campaign_target t ON t.id = s.target_id "
        "WHERE t.campaign_id = ? AND s.sent_at IS NOT NULL",
        (campaign_id,),
    )["n"]

    responses = db.query_one(
        "SELECT COUNT(*) AS n FROM response r JOIN campaign_target t ON t.id = r.target_id "
        "WHERE t.campaign_id = ?",
        (campaign_id,),
    )["n"]

    placed = outcomes.active_placement_count(campaign_id)

    ready = sum(counts.get(status, 0) for status in
                (campaigns.READY, campaigns.DRAFTED, campaigns.NEEDS_APPROVAL,
                 campaigns.APPROVED, campaigns.QUEUED))

    return {
        "discovered": total,
        "high_fit": high_fit,
        "verified": verified,
        "ready": ready,
        "submitted": submitted,
        "responses": responses,
        "placed": placed,
        "qualified": counts.get(campaigns.QUALIFIED, 0),
        "duplicates": counts.get(campaigns.DUPLICATE, 0),
        "disqualified": counts.get(campaigns.DISQUALIFIED, 0),
        "blocked": counts.get(campaigns.BLOCKED, 0),
        "opted_out": counts.get(campaigns.OPTED_OUT, 0),
        "bounced": counts.get(campaigns.BOUNCED, 0),
        "response_rate": round(responses / submitted * 100) if submitted else None,
        "placement_rate": round(placed / submitted * 100) if submitted else None,
    }


def campaign_health(campaign_id):
    """A 0–100 health figure built from four measurable factors."""
    metrics = campaign_metrics(campaign_id)
    budget = campaigns.budget(campaign_id) or {}
    campaign = campaigns.get(campaign_id)
    progress = jobs.progress_for_campaign(campaign_id)

    factors = []

    discovered = metrics["discovered"]
    qualified = metrics["qualified"] + metrics["ready"]
    yield_score = min(1.0, (qualified / discovered)) if discovered else 0.0
    factors.append(("Qualified yield", yield_score, 0.3,
                    f"{qualified} of {discovered} discovered"))

    duplicate_rate = (metrics["duplicates"] / discovered) if discovered else 0.0
    factors.append(("Duplicate control", max(0.0, 1.0 - duplicate_rate * 2), 0.2,
                    f"{metrics['duplicates']} duplicates consolidated"))

    verification = (metrics["verified"] / discovered) if discovered else 0.0
    factors.append(("Contact verification", min(1.0, verification * 2), 0.3,
                    f"{metrics['verified']} independently verified routes"))

    failure_rate = (progress["failed"] / progress["total"]) if progress["total"] else 0.0
    factors.append(("Job reliability", max(0.0, 1.0 - failure_rate), 0.2,
                    f"{progress['failed']} failed of {progress['total']} jobs"))

    weight_total = sum(weight for _, _, weight, _ in factors)
    score = round(sum(value * weight for _, value, weight, _ in factors)
                  / weight_total * 100) if weight_total else 0

    return {
        "score": score,
        "factors": [{"label": label, "score": round(value * 100), "weight": weight,
                     "detail": detail} for label, value, weight, detail in factors],
        "budget": budget,
        "search_budget": campaign["search_budget"] if campaign else None,
        "progress": progress,
    }


def placement_value(placement_id, tenant_id=None):
    """A placement's value score. Streams alone never drive it."""
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    placement = db.query_one("SELECT * FROM placement WHERE id = ?", (placement_id,))
    if placement is None:
        return None

    components = {}
    outcome_rows = db.query(
        "SELECT metric, value FROM analytics_snapshot WHERE placement_id = ? AND kind = ?",
        (placement_id, OUTCOME),
    )
    measured = {row["metric"]: row["value"] for row in outcome_rows}

    for metric, weight in [("unique_listeners", 0.25), ("saves", 0.2), ("follows", 0.15),
                           ("repeat_listens", 0.15), ("downstream_discovery", 0.1)]:
        components[metric] = {
            "value": measured.get(metric),
            "weight": weight,
            "known": metric in measured,
        }

    duration_days = None
    if placement["added_date"]:
        end = placement["removed_date"] or clock.now_iso()[:10]
        start = clock.parse(f"{placement['added_date']}T00:00:00+00:00")
        finish = clock.parse(f"{end}T00:00:00+00:00")
        if start and finish:
            duration_days = max(0, round((finish - start).total_seconds() / 86400))
    components["duration_days"] = {"value": duration_days, "weight": 0.15,
                                   "known": duration_days is not None}

    known = [item for item in components.values() if item["known"] and item["value"]]
    if not known:
        return {
            "placement_id": placement_id,
            "score": None,
            "state": "UNMEASURED",
            "components": components,
            "cost": placement["cost"],
            "note": "No outcome measurements have been recorded for this placement yet. "
                    "REACH reports UNKNOWN rather than estimating a value.",
        }

    weight_total = sum(item["weight"] for item in known)
    normalized = sum(min(1.0, (item["value"] or 0) / 1000) * item["weight"] for item in known)
    score = round(normalized / weight_total * 100) if weight_total else 0

    return {
        "placement_id": placement_id,
        "score": score,
        "state": "MEASURED",
        "components": components,
        "cost": placement["cost"],
        "cost_per_point": round(placement["cost"] / score, 2) if score and placement["cost"] else None,
        "note": "Score reflects only the metrics actually recorded.",
    }


def attribution_report(campaign_id, tenant_id=None):
    """Compare baseline and outcome without asserting causation."""
    tenant_id = tenant_id or rbac.current_principal().tenant_id
    rows = []
    for metric in ("streams", "total_earned"):
        baseline = baseline_for(campaign_id, metric, tenant_id)
        outcome = db.query_one(
            "SELECT * FROM analytics_snapshot WHERE tenant_id = ? AND campaign_id = ? "
            "AND kind = ? AND metric = ? ORDER BY captured_at DESC LIMIT 1",
            (tenant_id, campaign_id, OUTCOME, metric),
        )
        if baseline is None:
            rows.append({"metric": metric, "state": "NO_BASELINE",
                         "note": "No baseline was captured before outreach started."})
            continue
        if outcome is None:
            rows.append({"metric": metric, "baseline": baseline["value"],
                         "state": "NO_OUTCOME",
                         "note": "No post-placement measurement has been recorded."})
            continue
        delta = outcome["value"] - baseline["value"]
        rows.append({
            "metric": metric,
            "baseline": baseline["value"],
            "outcome": outcome["value"],
            "delta": delta,
            "attribution": OBSERVED_CORRELATION,
            "state": "MEASURED",
            "note": "Observed change over the campaign window. REACH does not claim "
                    "the campaign caused this change without per-source referral data.",
        })
    return rows


def observability(tenant_id=None):
    """Operational counters for the Provider Health screen."""
    tenant_id = tenant_id or rbac.current_principal().tenant_id

    def scalar(sql, params=()):
        row = db.query_one(sql, params)
        return row["n"] if row else 0

    provider_rows = db.query(
        "SELECT provider, COUNT(*) AS calls, "
        "SUM(cost_units) AS units, "
        "SUM(CASE WHEN outcome = 'OK' THEN 1 ELSE 0 END) AS ok, "
        "SUM(CASE WHEN outcome = 'RATE_LIMITED' THEN 1 ELSE 0 END) AS rate_limited, "
        "SUM(CASE WHEN outcome = 'ERROR' THEN 1 ELSE 0 END) AS errors "
        "FROM provider_request_log WHERE tenant_id = ? GROUP BY provider ORDER BY provider",
        (tenant_id,),
    )

    fetched = scalar("SELECT COUNT(*) AS n FROM source_document WHERE tenant_id = ?",
                     (tenant_id,))
    blocked = scalar("SELECT COUNT(*) AS n FROM source_document WHERE tenant_id = ? "
                     "AND fetch_status = 'BLOCKED'", (tenant_id,))
    targets = scalar("SELECT COUNT(*) AS n FROM campaign_target WHERE tenant_id = ?",
                     (tenant_id,))
    duplicates = scalar("SELECT COUNT(*) AS n FROM campaign_target WHERE tenant_id = ? "
                        "AND status = 'DUPLICATE'", (tenant_id,))
    qualified = scalar("SELECT COUNT(*) AS n FROM campaign_target WHERE tenant_id = ? "
                       "AND status IN ('QUALIFIED','READY')", (tenant_id,))
    stale = len(evidence.stale_documents(tenant_id))

    sends = scalar("SELECT COUNT(*) AS n FROM submission WHERE tenant_id = ? "
                   "AND sent_at IS NOT NULL", (tenant_id,))
    bounces = scalar("SELECT COUNT(*) AS n FROM submission WHERE tenant_id = ? "
                     "AND status = 'BOUNCED'", (tenant_id,))
    opt_outs = scalar("SELECT COUNT(*) AS n FROM response WHERE tenant_id = ? "
                      "AND kind = 'OPT_OUT'", (tenant_id,))
    replies = scalar("SELECT COUNT(*) AS n FROM response WHERE tenant_id = ?", (tenant_id,))

    job_rows = db.query(
        "SELECT status, COUNT(*) AS n FROM job_run WHERE tenant_id = ? GROUP BY status",
        (tenant_id,),
    )

    return {
        "providers": [dict(row) for row in provider_rows],
        "pages_fetched": fetched,
        "pages_blocked": blocked,
        "targets": targets,
        "duplicate_rate": round(duplicates / targets * 100) if targets else 0,
        "qualified_yield": round(qualified / targets * 100) if targets else 0,
        "stale_sources": stale,
        "emails_sent": sends,
        "bounces": bounces,
        "opt_outs": opt_outs,
        "replies": replies,
        "model_calls": 0,
        "model_tokens": 0,
        "jobs": {row["status"]: row["n"] for row in job_rows},
    }
