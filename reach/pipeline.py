"""Deep discovery pipeline.

Nine passes, each one a durable job, so a campaign can be paused, resumed and
inspected mid-flight:

1. query planning      5. curator/organization resolution
2. search              6. contact and route resolution
3. focused fetch       7. verification
4. extraction          8. qualification
                       9. diminishing-return stop

Discovery is read-only. Nothing in this module can send a message, spend money
or change external state — the research path has no access to those tools at all.
"""

import json
import time

from . import (audit, campaigns, clock, compliance, contacts, crypto, db, entities,
               evidence, extractor, fetcher, jobs, netguard, policy, profile,
               sanitizer, scoring)
from .errors import BudgetExceeded, FetchBlocked, ProviderNotConnected
from .providers import search as search_provider

PIPELINE_VERSION = "pipeline/1.0.0"

# --- pass 1: query planning ------------------------------------------------

# Local-language submission phrasing for the territories Phase One supports.
LANGUAGE_TEMPLATES = {
    "DE": ["Musik einreichen {genre}", "{genre} Blog Musik einsenden", "{genre} Radio Demo einreichen"],
    "FR": ["soumettre musique {genre}", "{genre} blog envoyer musique", "démo radio {genre}"],
    "ES": ["enviar música {genre}", "{genre} blog enviar demo", "radio {genre} enviar música"],
    "IT": ["inviare musica {genre}", "{genre} blog invia demo"],
    "NL": ["muziek insturen {genre}", "{genre} blog demo insturen"],
    "PT": ["enviar música {genre}", "{genre} blog enviar demo"],
    "BR": ["enviar música {genre}", "{genre} blog enviar demo"],
    "SE": ["skicka in musik {genre}"],
    "PL": ["wyślij muzykę {genre}"],
    "JP": ["音源 送る {genre}"],
}

CHANNEL_TEMPLATES = {
    "PLAYLIST": [
        "{genre} playlist submissions",
        "{microgenre} playlist curator submissions",
        "submit music to {genre} playlist",
    ],
    "CURATOR": [
        "{microgenre} curator submissions",
        "{genre} curator submit music",
    ],
    "RADIO": [
        "{genre} radio submissions",
        "college radio {genre} music submissions",
        "community radio submit {genre}",
    ],
    "BLOG": [
        "{genre} music blog submit music",
        "{microgenre} blog submission guidelines",
    ],
    "PUBLICATION": [
        "{genre} magazine premiere submissions",
        "{genre} music publication editorial contact",
    ],
    "DJ": [
        "{genre} DJ promo submissions",
        "{microgenre} club DJ promo list",
    ],
    "DJ_POOL": [
        "{genre} DJ pool submit promo",
        "record pool {microgenre} promo",
    ],
    "PODCAST": [
        "{genre} podcast music submissions",
    ],
    "CREATOR": [
        "{genre} creator business enquiries music",
    ],
}


def plan_queries(campaign_id, limit=None):
    """Pass 1. Build query families from the track profile and campaign settings.

    Queries come from real profile values only. A field the artist did not
    supply produces no query rather than a guessed one.
    """
    campaign = campaigns.get(campaign_id)
    settings = campaigns.settings(campaign_id)
    profile_id = profile.get_or_create(campaign["recording_id"])
    values = profile.values(profile_id)

    genres = [values.get("primary_genre")] + list(values.get("secondary_genres") or [])
    genres = [genre for genre in genres if genre]
    microgenres = [item for item in (values.get("microgenres") or []) if item]
    comparables = [item for item in (values.get("comparable_artists") or []) if item]
    scene = values.get("cultural_scene")
    formats = values.get("radio_formats") or []

    # Queries are grouped by family and then interleaved, so a large channel
    # list cannot consume the whole budget before the territory and
    # local-language families get a turn.
    families = {}
    seen = set()

    def add(text, family, territory=None, language="en"):
        text = " ".join(str(text).split())
        if not text or "{" in text:
            return
        key = text.lower()
        if key in seen:
            return
        seen.add(key)
        families.setdefault(family, []).append({
            "query": text, "family": family, "territory": territory, "language": language,
        })

    for channel in settings["channels"]:
        for template in CHANNEL_TEMPLATES.get(channel, []):
            for genre in genres[:2]:
                add(template.format(genre=genre, microgenre=genre), channel)
            for microgenre in microgenres[:3]:
                add(template.format(genre=microgenre, microgenre=microgenre), channel)

    for comparable in comparables[:3]:
        add(f"playlists featuring {comparable}", "COMPARABLE")
        add(f"{comparable} similar artists submissions", "COMPARABLE")

    if scene:
        add(f"{scene} scene submit music", "SCENE")

    for radio_format in formats[:2]:
        add(f"{radio_format} radio format submissions", "RADIO")

    for territory in settings["territories"]:
        templates = LANGUAGE_TEMPLATES.get(territory.upper(), [])
        for template in templates:
            for genre in (microgenres[:1] or genres[:1]):
                add(template.format(genre=genre), "LOCAL_LANGUAGE", territory,
                    territory.lower())
        for genre in (microgenres[:1] or genres[:1]):
            add(f"{territory} {genre} playlist curator", "TERRITORY", territory)

    budget = limit or campaign["search_budget"]
    ordered = []
    buckets = [list(items) for _family, items in sorted(families.items())]
    while buckets and len(ordered) < budget:
        for bucket in list(buckets):
            if not bucket:
                buckets.remove(bucket)
                continue
            ordered.append(bucket.pop(0))
            if len(ordered) >= budget:
                break
    return ordered


# --------------------------------------------------------------------------
# job handlers
# --------------------------------------------------------------------------

@jobs.register("PLAN_DISCOVERY")
def _plan_discovery(context):
    campaign_id = context.campaign_id
    queries = plan_queries(campaign_id)
    context.progress(0, len(queries), "Planning discovery queries")
    for index, query in enumerate(queries):
        context.enqueue("SEARCH_PROVIDER", {"query": query, "index": index},
                        idempotency_key=f"search:{campaign_id}:{query['query']}")
    campaigns.set_status(campaign_id, campaigns.ACTIVE)
    audit.record("discovery.planned", entity_type="campaign", entity_id=campaign_id,
                 payload={"queries": len(queries)})
    return {"queries": len(queries)}


@jobs.register("SEARCH_PROVIDER")
def _search_provider(context):
    if context.cancelled():
        return None
    campaign_id = context.campaign_id
    campaign = campaigns.get(campaign_id)
    budget = campaigns.budget(campaign_id)

    if budget["searches_used"] >= campaign["search_budget"]:
        raise BudgetExceeded(
            f"Campaign search budget exhausted ({budget['searches_used']}/"
            f"{campaign['search_budget']})"
        )

    query = context.payload["query"]
    response = search_provider.search(query["query"], limit=8, campaign_id=campaign_id)
    campaigns.spend_budget(campaign_id, searches=1)
    context.cost(searches=1)

    enqueued = 0
    platform_skipped = 0
    for result in response.items:
        url = result.get("url")
        if not url:
            continue
        try:
            validated = netguard.validate_url(url, resolve=False)
        except FetchBlocked:
            continue
        domain = validated["domain"]
        if entities.is_platform_domain(domain):
            platform_skipped += 1
            continue
        if not campaigns.domain_budget_available(campaign_id, domain):
            continue
        campaigns.spend_budget(campaign_id, domain=domain)
        context.enqueue("FETCH_PUBLIC_PAGE", {
            "url": url,
            "query": query["query"],
            "family": query["family"],
            "territory": query.get("territory"),
            "rank": result.get("rank"),
            "search_mode": response.mode,
        }, idempotency_key=f"fetch:{campaign_id}:{url}")
        enqueued += 1

    return {
        "query": query["query"],
        "results": len(response.items),
        "fetch_jobs": enqueued,
        "platform_skipped": platform_skipped,
        "mode": response.mode,
        "note": response.note,
    }


@jobs.register("FETCH_PUBLIC_PAGE")
def _fetch_public_page(context):
    """Passes 3 and 4: focused fetch, sanitize, extract."""
    if context.cancelled():
        return None
    campaign_id = context.campaign_id
    url = context.payload["url"]

    try:
        result = fetcher.fetch(url)
    except FetchBlocked as exc:
        evidence.record_source_document(
            url=url, domain=netguard.registrable_domain(_hostname(url)),
            provider="open_web_fetch", fetch_status=evidence.FETCH_BLOCKED,
            campaign_id=campaign_id, robots_decision="N/A", block_reason=exc.reason,
        )
        audit.record("fetch.blocked", entity_type="source_document", entity_id=None,
                     payload={"url": url, "reason": exc.reason})
        return {"blocked": exc.reason, "url": url}

    campaigns.spend_budget(campaign_id, pages=1)
    context.cost(pages=1)

    sanitized = sanitizer.sanitize(result.text(), base_url=result.final_url)
    extracted = extractor.extract(sanitized, result.final_url, result.domain,
                                  http_status=result.status)

    if extracted["injection_findings"]:
        # Recorded as a risk signal on the source, never acted on.
        audit.record("security.injection_detected", entity_type="source_document",
                     entity_id=None, payload={"url": result.final_url,
                                              "findings": extracted["injection_findings"]})

    document_id = evidence.record_source_document(
        url=result.final_url, domain=result.domain, provider="open_web_fetch",
        fetch_status=evidence.FETCH_OK, campaign_id=campaign_id,
        http_status=result.status, mime=result.mime, byte_count=result.bytes,
        sanitized_text=sanitized["visible_text"], title=sanitized["title"],
        classification=extracted["classification"], robots_decision=result.robots_decision,
    )

    if extracted["page_state"] != extractor.PAGE_OK:
        # A bot challenge or an error page is not the site behind it. The fetch
        # is recorded as evidence of the attempt, but nothing downstream may
        # classify, name or qualify an outlet from an interstitial — the first
        # real run turned eight of these into "curators" called "Just a
        # moment...".
        audit.record("fetch.page_unusable", entity_type="source_document",
                     entity_id=document_id,
                     payload={"url": result.final_url,
                              "page_state": extracted["page_state"],
                              "http_status": result.status})
        return {"url": result.final_url, "page_state": extracted["page_state"],
                "document_id": document_id, "skipped": True}

    context.enqueue("RESOLVE_ENTITY", {
        "document_id": document_id,
        "extracted": extracted,
        "territory": context.payload.get("territory"),
    }, idempotency_key=f"resolve:{campaign_id}:{document_id}")

    return {
        "url": result.final_url,
        "classification": extracted["classification"],
        "injection_findings": extracted["injection_findings"],
        "document_id": document_id,
    }


def _hostname(url):
    from urllib.parse import urlsplit

    return urlsplit(url).hostname or ""


@jobs.register("RESOLVE_ENTITY")
def _resolve_entity(context):
    """Passes 5 and 6: outlet/organization resolution, then contact and route."""
    if context.cancelled():
        return None
    campaign_id = context.campaign_id
    extracted = context.payload["extracted"]
    document_id = context.payload.get("document_id")
    territory = context.payload.get("territory")

    if extracted.get("page_state", extractor.PAGE_OK) != extractor.PAGE_OK:
        return {"skipped": extracted["page_state"]}
    if entities.is_platform_domain(extracted["domain"]):
        # Belt to the search-stage filter: a redirect can land on a platform
        # even when the search result did not start on one.
        return {"skipped": "PLATFORM_DOMAIN", "domain": extracted["domain"]}

    classification = extracted["classification"]
    kind = classification if classification in extractor.OUTLET_KINDS else _infer_kind(extracted)

    freshness_date = (extracted.get("freshness") or {}).get("date")
    last_activity = None
    if freshness_date and len(freshness_date) == 10:
        last_activity = f"{freshness_date}T00:00:00+00:00"

    resolved = contacts.record_from_extraction(
        extracted, source_document_id=document_id, provider="open_web_fetch",
        campaign_id=campaign_id,
    )

    outlet_id = entities.ensure_outlet(
        name=extracted["outlet_name"],
        url=extracted["url"],
        domain=extracted["domain"],
        kind=kind,
        description=extracted.get("description"),
        genres=extracted.get("genres"),
        territory=territory,
        submissions_open=extracted["submissions_open"],
        last_activity_at=last_activity,
        organization_id=resolved["organization_id"],
        profile=({"classification": classification,
                  "requires_login": extracted["requires_login"],
                  "requires_captcha": extracted["requires_captcha"]}),
    )

    classification_evidence = evidence.record(
        entity_type="outlet", entity_id=outlet_id, supports_field="classification",
        value=classification, source_url=extracted["url"],
        source_domain=extracted["domain"], source_type="OFFICIAL",
        excerpt=extracted.get("classification_excerpt"),
        confidence=extracted["classification_confidence"],
        extractor_version=extracted["extractor_version"], source_document_id=document_id,
    )
    if extracted["submissions_open"] != extractor.SUBMISSIONS_UNKNOWN:
        evidence.record(
            entity_type="outlet", entity_id=outlet_id, supports_field="submissions_open",
            value=extracted["submissions_open"], source_url=extracted["url"],
            source_domain=extracted["domain"], source_type="OFFICIAL",
            excerpt=extracted.get("submissions_excerpt"),
            confidence=extracted["submissions_confidence"],
            extractor_version=extracted["extractor_version"],
            source_document_id=document_id,
        )

    route_id, contact_method_id = _resolve_route(
        outlet_id, extracted, resolved, classification_evidence, document_id
    )

    target_id = campaigns.add_target(
        campaign_id, outlet_id,
        contact_id=resolved["contact_id"], route_id=route_id,
        status=campaigns.ENRICHING,
        dedup_key=entities.dedup_key(outlet_id, contact_method_id),
    )

    context.enqueue("VERIFY_SUBMISSION_ROUTE", {
        "target_id": target_id,
        "contact_method_id": contact_method_id,
        "extracted": extracted,
    }, idempotency_key=f"verify:{campaign_id}:{target_id}")

    return {"outlet_id": outlet_id, "target_id": target_id, "route_id": route_id}


def _infer_kind(extracted):
    mapping = {
        extractor.SUBMISSION_GUIDELINES: extractor.CURATOR,
        extractor.BUSINESS_CONTACT: extractor.CURATOR,
        extractor.PAID_CONSIDERATION: extractor.CURATOR,
        extractor.GUARANTEED_PLACEMENT: extractor.CURATOR,
        extractor.GUARANTEED_STREAMS: extractor.CURATOR,
    }
    return mapping.get(extracted["classification"], extractor.CURATOR)


def _resolve_route(outlet_id, extracted, resolved, evidence_id, document_id):
    """Pass 6. Prefer an explicit published email route; otherwise record what
    the page actually offers, including "login required" as a real answer."""
    sendable_method = None
    for method_id, category, _reasons in resolved["methods"]:
        if category in contacts.SENDABLE_CATEGORIES:
            sendable_method = method_id
            break

    cost_model = extracted["cost_model"]
    cost_amount = extracted["cost_amount"]
    currency = extracted["cost_currency"]

    if sendable_method:
        method = contacts.get_method(sendable_method)
        destination = contacts.reveal(sendable_method)
        route_id = entities.ensure_route(
            outlet_id, contacts.EMAIL, destination=destination,
            cost_model=cost_model, cost_amount=cost_amount, currency=currency,
            instructions=extracted.get("submissions_excerpt"),
            evidence_id=evidence_id, verified=bool(method["verified_at"]),
        )
        return route_id, sendable_method

    if extracted["requires_captcha"]:
        return entities.ensure_route(
            outlet_id, contacts.WEB_FORM, destination=extracted["url"],
            requires_captcha=True, cost_model=cost_model, cost_amount=cost_amount,
            currency=currency, instructions="CAPTCHA-protected form — human action required",
            evidence_id=evidence_id,
        ), None

    if extracted["requires_login"]:
        return entities.ensure_route(
            outlet_id, contacts.LOGIN_REQUIRED, destination=extracted["url"],
            requires_login=True, cost_model=cost_model, cost_amount=cost_amount,
            currency=currency, instructions="Login-walled submission — human action required",
            evidence_id=evidence_id,
        ), None

    if extracted["submission_links"]:
        return entities.ensure_route(
            outlet_id, contacts.WEB_FORM,
            destination=extracted["submission_links"][0]["href"],
            cost_model=cost_model, cost_amount=cost_amount, currency=currency,
            instructions=extracted.get("submissions_excerpt"), evidence_id=evidence_id,
        ), None

    return entities.ensure_route(
        outlet_id, contacts.UNSUPPORTED, destination=extracted["url"],
        cost_model=cost_model, cost_amount=cost_amount, currency=currency,
        instructions="No published submission route found", evidence_id=evidence_id,
    ), None


@jobs.register("VERIFY_SUBMISSION_ROUTE")
def _verify_route(context):
    """Pass 7. Everything that has to be true before an opportunity qualifies."""
    if context.cancelled():
        return None
    target_id = context.payload["target_id"]
    extracted = context.payload["extracted"]
    contact_method_id = context.payload.get("contact_method_id")

    target = campaigns.get_target(target_id)
    if target is None:
        return None

    checks = []

    def check(key, ok, detail):
        checks.append({"key": key, "ok": bool(ok), "detail": detail})
        return ok

    document = db.query_one(
        "SELECT * FROM source_document WHERE url_hash = ? AND campaign_id = ?",
        (crypto.url_hash(extracted["url"]), context.campaign_id),
    )
    check("page_exists", document is not None and document["fetch_status"] == evidence.FETCH_OK,
          "Source page was retrieved successfully")
    check("submissions_open", extracted["submissions_open"] != extractor.CLOSED,
          f"Submission state: {extracted['submissions_open']}")

    freshness = extracted.get("freshness") or {}
    freshness_days = None
    if freshness.get("date"):
        raw = freshness["date"]
        freshness_days = clock.days_since(raw if len(raw) > 4 else f"{raw}-01-01")
    check("recent", freshness_days is None or freshness_days <= 365,
          f"Last dated activity: {freshness.get('date') or 'UNKNOWN'}")

    route = entities.best_route(target["outlet_id"])
    check("route_consistent", route is not None and route["method"] != contacts.UNSUPPORTED,
          f"Route: {route['method'] if route else 'NONE'}")

    contact_state = contacts.sendability(contact_method_id) if contact_method_id else None
    check("contact_verified", bool(contact_state and contact_state["sendable"]),
          contact_state["blockers"][0] if contact_state and contact_state["blockers"]
          else "Contact is independently verified" if contact_state else "No contact route")

    if contact_method_id:
        evidence_rows = contacts.method_evidence(contact_method_id)
        check("independently_sourced", len(evidence_rows) >= 1,
              f"{len(evidence_rows)} evidence packet(s) support this address")

    context.enqueue("DEDUPLICATE", {
        "target_id": target_id, "extracted": extracted,
        "contact_method_id": contact_method_id,
        "freshness_days": freshness_days, "checks": checks,
    }, idempotency_key=f"dedup:{context.campaign_id}:{target_id}")

    return {"target_id": target_id, "checks": checks}


@jobs.register("DEDUPLICATE")
def _deduplicate(context):
    if context.cancelled():
        return None
    target_id = context.payload["target_id"]
    target = campaigns.get_target(target_id)
    if target is None:
        return None

    key = target["dedup_key"]
    duplicate_of = entities.find_duplicate_target(context.campaign_id, key, target_id)
    if duplicate_of is not None:
        campaigns.set_target_status(
            target_id, campaigns.DUPLICATE,
            reason=f"Same submission route as {duplicate_of['outlet_id']}",
            rejection_reason="Duplicate",
        )
        related = json.loads(duplicate_of["related_outlets_json"] or "[]")
        if target["outlet_id"] not in related:
            related.append(target["outlet_id"])
            db.update("campaign_target", duplicate_of["id"], {
                "related_outlets_json": json.dumps(related),
                "updated_at": clock.now_iso(),
            })
        return {"duplicate_of": duplicate_of["id"], "consolidated": True}

    context.enqueue("SCORE_OPPORTUNITY", {
        "target_id": target_id,
        "extracted": context.payload["extracted"],
        "contact_method_id": context.payload.get("contact_method_id"),
        "freshness_days": context.payload.get("freshness_days"),
    }, idempotency_key=f"score:{context.campaign_id}:{target_id}")
    return {"duplicate_of": None}


@jobs.register("SCORE_OPPORTUNITY")
def _score_opportunity(context):
    """Pass 8, part one: REACH score."""
    if context.cancelled():
        return None
    target_id = context.payload["target_id"]
    target = campaigns.get_target(target_id)
    campaign = campaigns.get(context.campaign_id)
    outlet = entities.get_outlet(target["outlet_id"])
    profile_id = profile.get_or_create(campaign["recording_id"])
    values = profile.values(profile_id)

    contact_method_id = context.payload.get("contact_method_id")
    contact_state = contacts.sendability(contact_method_id) if contact_method_id else None
    relationship = _relationship_for(target)

    result = scoring.score_opportunity(
        outlet, values, campaign, contact_state=contact_state,
        relationship=relationship, freshness_days=context.payload.get("freshness_days"),
    )
    scoring.persist_score(target_id, result)

    context.enqueue("ASSESS_RISK", {
        "target_id": target_id,
        "extracted": context.payload["extracted"],
        "contact_method_id": contact_method_id,
    }, idempotency_key=f"risk:{context.campaign_id}:{target_id}")
    return {"score": result["score"]}


def _relationship_for(target):
    return db.query_one(
        "SELECT * FROM relationship WHERE contact_id IS ? AND outlet_id = ?",
        (target["contact_id"], target["outlet_id"]),
    )


@jobs.register("ASSESS_RISK")
def _assess_risk(context):
    if context.cancelled():
        return None
    target_id = context.payload["target_id"]
    extracted = context.payload["extracted"]
    contact_method_id = context.payload.get("contact_method_id")
    contact_state = contacts.sendability(contact_method_id) if contact_method_id else None

    result = scoring.assess_risk(extracted, contact_state=contact_state)
    scoring.persist_risk(target_id, result)

    context.enqueue("ASSESS_COMPLIANCE", {
        "target_id": target_id,
        "contact_method_id": contact_method_id,
        "cost_model": extracted["cost_model"],
    }, idempotency_key=f"compliance:{context.campaign_id}:{target_id}")
    return {"risk": result["score"], "band": result["band"]}


@jobs.register("ASSESS_COMPLIANCE")
def _assess_compliance(context):
    """Pass 8, part two: compliance decision, then final qualification."""
    if context.cancelled():
        return None
    from . import sender

    target_id = context.payload["target_id"]
    target = campaigns.get_target(target_id)
    campaign = campaigns.get(context.campaign_id)
    outlet = entities.get_outlet(target["outlet_id"])
    risk_row = scoring.latest_risk(target_id)
    risk = {"score": risk_row["score"], "band": risk_row["band"]} if risk_row else None

    decision = compliance.decide(
        target_id=target_id,
        contact_method_id=context.payload.get("contact_method_id"),
        campaign=campaign, outlet=outlet,
        relationship=_relationship_for(target),
        cost_model=context.payload.get("cost_model"),
        risk=risk,
        sender_health=sender.health_summary(),
    )

    _qualify(target_id, decision, risk)
    return {"decision": decision["decision"]}


def _qualify(target_id, decision, risk):
    score_row = scoring.latest_score(target_id)
    score = score_row["score"] if score_row else 0
    target = campaigns.get_target(target_id)

    if decision["decision"] == compliance.BLOCK:
        campaigns.set_target_status(
            target_id, campaigns.BLOCKED,
            reason="; ".join(decision["reasons"][:2]),
            rejection_reason="Suspicious route" if risk and risk["band"] in
                             (scoring.BLOCK, scoring.HIGH) else "Invalid contact",
        )
        return campaigns.BLOCKED

    if target["submissions_open"] == extractor.CLOSED:
        campaigns.set_target_status(target_id, campaigns.DISQUALIFIED,
                                    reason="Submissions are closed",
                                    rejection_reason="Submissions closed")
        return campaigns.DISQUALIFIED

    if score < 35:
        campaigns.set_target_status(target_id, campaigns.DISQUALIFIED,
                                    reason=f"REACH score {score} below qualification threshold",
                                    rejection_reason="Not a genre fit")
        return campaigns.DISQUALIFIED

    route = entities.best_route(target["outlet_id"])
    needs_human = route is not None and route["method"] in (
        contacts.LOGIN_REQUIRED, contacts.WEB_FORM, contacts.UNSUPPORTED
    )

    if decision["decision"] in (compliance.DRAFT_ONLY, compliance.LEGAL_REVIEW) or needs_human:
        campaigns.set_target_status(target_id, campaigns.QUALIFIED,
                                    reason="Qualified — requires a human action")
        _create_human_task(target_id, route, decision)
        return campaigns.QUALIFIED

    campaigns.set_target_status(target_id, campaigns.READY,
                                reason="Qualified with a verified direct route")
    return campaigns.READY


def _create_human_task(target_id, route, decision):
    from . import humanactions

    humanactions.create_from_target(target_id, route, decision)


# --------------------------------------------------------------------------
# pass 9: diminishing returns
# --------------------------------------------------------------------------

def should_stop(campaign_id):
    """Why discovery should stop, or None to continue."""
    campaign = campaigns.get(campaign_id)
    if campaign is None:
        return "Campaign not found"
    if campaign["status"] == campaigns.CANCELLED:
        return "Campaign cancelled by user"

    budget = campaigns.budget(campaign_id) or {}
    if budget.get("searches_used", 0) >= campaign["search_budget"]:
        return (f"Search budget reached ({budget['searches_used']}/"
                f"{campaign['search_budget']})")

    counts = campaigns.status_counts(campaign_id)
    total = sum(counts.values())
    if total >= 20:
        duplicates = counts.get(campaigns.DUPLICATE, 0)
        if duplicates / total > 0.6:
            return f"Duplicate rate {round(duplicates / total * 100)}% — diminishing returns"
        qualified = counts.get(campaigns.QUALIFIED, 0) + counts.get(campaigns.READY, 0)
        if qualified / total < 0.05:
            return (f"Qualified yield {round(qualified / total * 100)}% — "
                    "diminishing returns")
    if policy.global_stop_engaged():
        return "Global emergency stop is engaged"
    return None


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def start(campaign_id):
    """Launch a Scout run. Read-only by construction."""
    from . import rbac

    rbac.require("campaign.run_discovery")
    campaign = campaigns.get(campaign_id)
    if campaign is None:
        raise ProviderNotConnected("Unknown campaign")

    job_id = jobs.enqueue(
        "PLAN_DISCOVERY", {"pipeline_version": PIPELINE_VERSION},
        idempotency_key=f"plan:{campaign_id}:{clock.now_iso()}",
        campaign_id=campaign_id,
    )
    audit.record("discovery.started", entity_type="campaign", entity_id=campaign_id,
                 payload={"job_id": job_id, "mode": campaign["mode"]},
                 actor_kind=audit.ACTOR_USER)
    return job_id


def run_to_completion(campaign_id, max_jobs=2000, max_seconds=None):
    """Drain jobs for this campaign, honouring the stop rules.

    ``max_seconds`` bounds one draining pass so a web request can chunk a long
    run instead of dying at the server's request timeout: the first real run
    was killed mid-flight at 120 seconds and needed manual re-taps to finish.
    Jobs a killed worker left claimed forever are requeued first.
    """
    jobs.requeue_stale()
    started = time.monotonic()
    processed = 0
    while processed < max_jobs:
        if max_seconds is not None and time.monotonic() - started >= max_seconds:
            break
        stop_reason = should_stop(campaign_id)
        if stop_reason and "budget" not in stop_reason.lower():
            audit.record("discovery.stopped", entity_type="campaign",
                         entity_id=campaign_id, payload={"reason": stop_reason})
            break
        job = jobs.run_one()
        if job is None:
            break
        processed += 1
    return processed
