"""REACH relational store.

REACH needs durable, queryable records — campaign state, evidence, suppression
and audit history all have to survive a restart, and Phase One requires that
"metrics are derived from real records". SQLite through the standard library
gives that: a genuine relational database with foreign keys, and no new runtime
dependency to deploy.

This is REACH's own store and its only store. There is no second database
behind it and no other application's data in it: the ``recording`` table is the
catalog, not a mirror of one.
"""

import os
import sqlite3
import threading
import uuid

from . import config

_local = threading.local()
_configured_path = None
_lock = threading.RLock()


def new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


# --------------------------------------------------------------------------
# migrations
# --------------------------------------------------------------------------

MIGRATIONS = [
    # 1 — tenancy, principals, audit, flags
    """
    CREATE TABLE tenant (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE principal (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenant(id),
        email TEXT NOT NULL,
        display_name TEXT,
        role TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (tenant_id, email)
    );
    CREATE TABLE audit_event (
        id TEXT PRIMARY KEY,
        seq INTEGER,
        tenant_id TEXT,
        actor_id TEXT,
        actor_kind TEXT NOT NULL,
        action TEXT NOT NULL,
        entity_type TEXT,
        entity_id TEXT,
        payload_json TEXT,
        created_at TEXT NOT NULL,
        prev_hash TEXT,
        hash TEXT NOT NULL
    );
    CREATE INDEX idx_audit_tenant ON audit_event(tenant_id, seq);
    """,
    # 2 — provider policy + connection state
    """
    CREATE TABLE provider_policy (
        provider TEXT PRIMARY KEY,
        policy_json TEXT NOT NULL,
        policy_version TEXT NOT NULL,
        last_verified_at TEXT,
        next_review_at TEXT,
        kill_switch_enabled INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE provider_connection (
        provider TEXT NOT NULL,
        tenant_id TEXT NOT NULL REFERENCES tenant(id),
        status TEXT NOT NULL,
        last_success_at TEXT,
        last_error TEXT,
        last_error_at TEXT,
        quota_used INTEGER NOT NULL DEFAULT 0,
        quota_limit INTEGER,
        quota_window_started_at TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (provider, tenant_id)
    );
    CREATE TABLE provider_request_log (
        id TEXT PRIMARY KEY,
        tenant_id TEXT,
        provider TEXT NOT NULL,
        operation TEXT NOT NULL,
        cost_units INTEGER NOT NULL DEFAULT 1,
        http_status INTEGER,
        outcome TEXT NOT NULL,
        campaign_id TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX idx_provider_req ON provider_request_log(provider, created_at);
    """,
    # 3 — music identity
    """
    CREATE TABLE artist (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenant(id),
        name TEXT NOT NULL,
        musicbrainz_artist_id TEXT,
        created_at TEXT NOT NULL,
        UNIQUE (tenant_id, name)
    );
    CREATE TABLE release (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenant(id),
        title TEXT NOT NULL,
        upc TEXT,
        release_date TEXT,
        musicbrainz_release_id TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE recording (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenant(id),
        artist_id TEXT NOT NULL REFERENCES artist(id),
        release_id TEXT REFERENCES release(id),
        slug TEXT,
        title TEXT NOT NULL,
        isrc TEXT,
        iswc TEXT,
        upc TEXT,
        musicbrainz_recording_id TEXT,
        version_type TEXT NOT NULL DEFAULT 'ORIGINAL',
        mix_name TEXT,
        edit_name TEXT,
        explicit INTEGER,
        duration_seconds INTEGER,
        master_version INTEGER NOT NULL DEFAULT 1,
        release_territory TEXT,
        release_date TEXT,
        -- First-party catalog facts, entered by the rights holder. NULL means
        -- UNKNOWN throughout: an unreported stream count is not a zero one, and
        -- "not told" is not the same answer as "no" for the two flags.
        publisher TEXT,
        writers_json TEXT,
        producers_json TEXT,
        alternate_titles_json TEXT,
        distributed INTEGER,
        splits_confirmed INTEGER,
        streams INTEGER,
        monthly_streams_json TEXT,
        reporting_platforms_json TEXT,
        is_sample INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        UNIQUE (tenant_id, slug, version_type, mix_name, edit_name)
    );
    CREATE TABLE platform_asset (
        id TEXT PRIMARY KEY,
        recording_id TEXT NOT NULL REFERENCES recording(id),
        provider TEXT NOT NULL,
        external_id TEXT,
        url TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE rights_attestation (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenant(id),
        recording_id TEXT NOT NULL REFERENCES recording(id),
        attested_by TEXT NOT NULL,
        scope_json TEXT NOT NULL,
        statement TEXT NOT NULL,
        attested_at TEXT NOT NULL,
        revoked_at TEXT
    );
    """,
    # 4 — track intelligence profile
    """
    CREATE TABLE track_profile (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenant(id),
        recording_id TEXT NOT NULL REFERENCES recording(id),
        version INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE track_profile_field (
        id TEXT PRIMARY KEY,
        profile_id TEXT NOT NULL REFERENCES track_profile(id),
        field TEXT NOT NULL,
        value_json TEXT,
        confidence REAL NOT NULL,
        source TEXT NOT NULL,
        extractor_version TEXT NOT NULL,
        generated_at TEXT NOT NULL,
        human_override INTEGER NOT NULL DEFAULT 0,
        UNIQUE (profile_id, field)
    );
    """,
    # 5 — discovery: source documents, evidence, outlets, routes
    """
    CREATE TABLE source_document (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenant(id),
        campaign_id TEXT,
        url TEXT NOT NULL,
        url_hash TEXT NOT NULL,
        domain TEXT NOT NULL,
        provider TEXT NOT NULL,
        fetch_status TEXT NOT NULL,
        http_status INTEGER,
        mime TEXT,
        bytes INTEGER,
        content_hash TEXT,
        sanitized_text TEXT,
        title TEXT,
        classification TEXT,
        robots_decision TEXT NOT NULL,
        block_reason TEXT,
        fetched_at TEXT,
        stale_at TEXT,
        created_at TEXT NOT NULL,
        UNIQUE (tenant_id, url_hash, campaign_id)
    );
    CREATE INDEX idx_source_domain ON source_document(domain);
    CREATE TABLE evidence_packet (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenant(id),
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        source_url TEXT NOT NULL,
        source_domain TEXT NOT NULL,
        source_type TEXT NOT NULL,
        excerpt TEXT,
        content_hash TEXT,
        supports_field TEXT NOT NULL,
        extracted_value_json TEXT,
        confidence REAL NOT NULL,
        retrieved_at TEXT NOT NULL,
        verified_at TEXT,
        stale_at TEXT,
        extractor_version TEXT NOT NULL,
        policy_decision_id TEXT,
        source_document_id TEXT REFERENCES source_document(id)
    );
    CREATE INDEX idx_evidence_entity ON evidence_packet(entity_type, entity_id);
    CREATE TABLE organization (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenant(id),
        name TEXT NOT NULL,
        domain TEXT,
        kind TEXT NOT NULL,
        official INTEGER NOT NULL DEFAULT 0,
        canonical_id TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE outlet (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenant(id),
        organization_id TEXT REFERENCES organization(id),
        kind TEXT NOT NULL,
        name TEXT NOT NULL,
        url TEXT,
        domain TEXT,
        provider TEXT NOT NULL,
        external_id TEXT,
        territory TEXT,
        languages_json TEXT,
        genres_json TEXT,
        description TEXT,
        submissions_open TEXT NOT NULL DEFAULT 'UNKNOWN',
        follower_count INTEGER,
        follower_count_source TEXT,
        last_activity_at TEXT,
        profile_json TEXT,
        canonical_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX idx_outlet_domain ON outlet(tenant_id, domain);
    CREATE TABLE submission_route (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenant(id),
        outlet_id TEXT NOT NULL REFERENCES outlet(id),
        method TEXT NOT NULL,
        destination TEXT,
        provider TEXT,
        requires_login INTEGER NOT NULL DEFAULT 0,
        requires_captcha INTEGER NOT NULL DEFAULT 0,
        cost_model TEXT NOT NULL DEFAULT 'UNKNOWN',
        cost_amount REAL,
        currency TEXT,
        instructions TEXT,
        evidence_id TEXT REFERENCES evidence_packet(id),
        verified_at TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        broken_reported_at TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE entity_merge (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenant(id),
        entity_type TEXT NOT NULL,
        winner_id TEXT NOT NULL,
        loser_id TEXT NOT NULL,
        rule TEXT NOT NULL,
        merged_at TEXT NOT NULL,
        undone_at TEXT
    );
    """,
    # 6 — contacts, suppression, relationships
    """
    CREATE TABLE professional_contact (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenant(id),
        organization_id TEXT REFERENCES organization(id),
        display_name TEXT,
        role TEXT,
        canonical_id TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE contact_method (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenant(id),
        contact_id TEXT NOT NULL REFERENCES professional_contact(id),
        kind TEXT NOT NULL,
        category TEXT NOT NULL,
        value_ciphertext TEXT NOT NULL,
        value_hash TEXT NOT NULL,
        value_preview TEXT,
        domain TEXT,
        role_based INTEGER NOT NULL DEFAULT 0,
        spotify_only INTEGER NOT NULL DEFAULT 0,
        independent_source_count INTEGER NOT NULL DEFAULT 0,
        verified_at TEXT,
        mx_checked_at TEXT,
        mx_ok INTEGER,
        created_at TEXT NOT NULL,
        UNIQUE (tenant_id, value_hash)
    );
    CREATE TABLE contact_evidence (
        id TEXT PRIMARY KEY,
        contact_method_id TEXT NOT NULL REFERENCES contact_method(id),
        evidence_id TEXT NOT NULL REFERENCES evidence_packet(id),
        UNIQUE (contact_method_id, evidence_id)
    );
    CREATE TABLE suppression_record (
        id TEXT PRIMARY KEY,
        tenant_id TEXT,
        value_hash TEXT NOT NULL,
        scope TEXT NOT NULL,
        reason TEXT NOT NULL,
        source TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX idx_suppression_hash ON suppression_record(value_hash);
    CREATE TABLE relationship (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenant(id),
        contact_id TEXT REFERENCES professional_contact(id),
        outlet_id TEXT REFERENCES outlet(id),
        owner_principal_id TEXT REFERENCES principal(id),
        preferred_method TEXT,
        last_contact_at TEXT,
        next_eligible_at TEXT,
        accepted_count INTEGER NOT NULL DEFAULT 0,
        declined_count INTEGER NOT NULL DEFAULT 0,
        placement_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (tenant_id, contact_id, outlet_id)
    );
    CREATE TABLE relationship_note (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenant(id),
        relationship_id TEXT NOT NULL REFERENCES relationship(id),
        body TEXT NOT NULL,
        author_id TEXT,
        created_at TEXT NOT NULL
    );
    """,
    # 7 — campaigns and outreach
    """
    CREATE TABLE campaign (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenant(id),
        recording_id TEXT NOT NULL REFERENCES recording(id),
        rights_attestation_id TEXT REFERENCES rights_attestation(id),
        name TEXT NOT NULL,
        mode TEXT NOT NULL,
        status TEXT NOT NULL,
        territories_json TEXT NOT NULL,
        channels_json TEXT NOT NULL,
        languages_json TEXT,
        start_date TEXT,
        end_date TEXT,
        search_budget INTEGER NOT NULL,
        domain_budget INTEGER NOT NULL,
        daily_send_limit INTEGER NOT NULL,
        priorities_json TEXT,
        created_by TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE campaign_target (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenant(id),
        campaign_id TEXT NOT NULL REFERENCES campaign(id),
        outlet_id TEXT NOT NULL REFERENCES outlet(id),
        contact_id TEXT REFERENCES professional_contact(id),
        route_id TEXT REFERENCES submission_route(id),
        status TEXT NOT NULL,
        status_reason TEXT,
        owner_principal_id TEXT,
        dedup_key TEXT,
        related_outlets_json TEXT,
        tags_json TEXT,
        rejection_reason TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (campaign_id, outlet_id)
    );
    CREATE INDEX idx_target_dedup ON campaign_target(campaign_id, dedup_key);
    CREATE TABLE opportunity_score (
        id TEXT PRIMARY KEY,
        target_id TEXT NOT NULL REFERENCES campaign_target(id),
        score INTEGER NOT NULL,
        components_json TEXT NOT NULL,
        reasons_json TEXT NOT NULL,
        version TEXT NOT NULL,
        human_override INTEGER NOT NULL DEFAULT 0,
        override_reason TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE risk_assessment (
        id TEXT PRIMARY KEY,
        target_id TEXT NOT NULL REFERENCES campaign_target(id),
        score INTEGER NOT NULL,
        band TEXT NOT NULL,
        signals_json TEXT NOT NULL,
        version TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE compliance_decision (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenant(id),
        target_id TEXT REFERENCES campaign_target(id),
        campaign_id TEXT REFERENCES campaign(id),
        contact_id TEXT REFERENCES professional_contact(id),
        decision TEXT NOT NULL,
        record_json TEXT NOT NULL,
        policy_version TEXT NOT NULL,
        decided_at TEXT NOT NULL
    );
    CREATE TABLE outreach_draft (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenant(id),
        target_id TEXT NOT NULL REFERENCES campaign_target(id),
        recipient_preview TEXT,
        recipient_hash TEXT,
        subject TEXT NOT NULL,
        body TEXT NOT NULL,
        links_json TEXT NOT NULL,
        attachments_json TEXT NOT NULL,
        language TEXT NOT NULL,
        translation_flagged INTEGER NOT NULL DEFAULT 0,
        facts_json TEXT NOT NULL,
        generator_version TEXT NOT NULL,
        payload_hash TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE approval (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenant(id),
        draft_id TEXT NOT NULL REFERENCES outreach_draft(id),
        target_id TEXT NOT NULL REFERENCES campaign_target(id),
        approved_by TEXT NOT NULL,
        approved_at TEXT NOT NULL,
        payload_hash TEXT NOT NULL,
        cost REAL NOT NULL DEFAULT 0,
        invalidated_at TEXT,
        invalidation_reason TEXT
    );
    CREATE TABLE submission (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenant(id),
        target_id TEXT NOT NULL REFERENCES campaign_target(id),
        route_id TEXT REFERENCES submission_route(id),
        approval_id TEXT REFERENCES approval(id),
        method TEXT NOT NULL,
        provider TEXT,
        provider_message_id TEXT,
        payload_hash TEXT NOT NULL,
        status TEXT NOT NULL,
        cost REAL NOT NULL DEFAULT 0,
        sent_at TEXT,
        recorded_by TEXT,
        error TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE response (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenant(id),
        target_id TEXT NOT NULL REFERENCES campaign_target(id),
        submission_id TEXT REFERENCES submission(id),
        kind TEXT NOT NULL,
        sentiment TEXT,
        body_excerpt TEXT,
        received_at TEXT NOT NULL,
        recorded_by TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE follow_up (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenant(id),
        target_id TEXT NOT NULL REFERENCES campaign_target(id),
        sequence INTEGER NOT NULL,
        due_at TEXT NOT NULL,
        status TEXT NOT NULL,
        sent_at TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE placement (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenant(id),
        campaign_id TEXT REFERENCES campaign(id),
        target_id TEXT REFERENCES campaign_target(id),
        outlet_id TEXT NOT NULL REFERENCES outlet(id),
        recording_id TEXT NOT NULL REFERENCES recording(id),
        url TEXT,
        program_name TEXT,
        added_date TEXT,
        verified_at TEXT,
        removed_date TEXT,
        position INTEGER,
        evidence_id TEXT NOT NULL REFERENCES evidence_packet(id),
        evidence_source TEXT NOT NULL,
        confidence REAL NOT NULL,
        cost REAL NOT NULL DEFAULT 0,
        notes TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE analytics_snapshot (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenant(id),
        campaign_id TEXT REFERENCES campaign(id),
        recording_id TEXT REFERENCES recording(id),
        placement_id TEXT REFERENCES placement(id),
        kind TEXT NOT NULL,
        metric TEXT NOT NULL,
        value REAL NOT NULL,
        source TEXT NOT NULL,
        attribution TEXT NOT NULL,
        captured_at TEXT NOT NULL
    );
    CREATE TABLE human_action_task (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenant(id),
        campaign_id TEXT REFERENCES campaign(id),
        target_id TEXT REFERENCES campaign_target(id),
        provider TEXT NOT NULL,
        title TEXT NOT NULL,
        action TEXT NOT NULL,
        reason TEXT NOT NULL,
        destination_url TEXT,
        deadline TEXT,
        eligibility_json TEXT,
        fields_json TEXT,
        assets_json TEXT,
        pitch_text TEXT,
        effort TEXT,
        cost REAL NOT NULL DEFAULT 0,
        currency TEXT,
        risk_band TEXT,
        status TEXT NOT NULL,
        snooze_until TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE sender_identity (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenant(id),
        from_email TEXT,
        from_name TEXT,
        domain TEXT,
        postal_address TEXT,
        provider TEXT,
        checks_json TEXT NOT NULL,
        status TEXT NOT NULL,
        checked_at TEXT NOT NULL
    );
    """,
    # 8 — durable jobs
    """
    CREATE TABLE job_run (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenant(id),
        campaign_id TEXT REFERENCES campaign(id),
        kind TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 3,
        progress INTEGER NOT NULL DEFAULT 0,
        total INTEGER NOT NULL DEFAULT 0,
        message TEXT,
        error TEXT,
        error_kind TEXT,
        result_json TEXT,
        cost_json TEXT,
        cancelled INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT,
        created_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT
    );
    CREATE INDEX idx_job_status ON job_run(status, next_attempt_at);
    CREATE INDEX idx_job_campaign ON job_run(campaign_id, created_at);
    CREATE TABLE campaign_budget (
        campaign_id TEXT PRIMARY KEY REFERENCES campaign(id),
        searches_used INTEGER NOT NULL DEFAULT 0,
        pages_fetched INTEGER NOT NULL DEFAULT 0,
        model_tokens INTEGER NOT NULL DEFAULT 0,
        emails_sent INTEGER NOT NULL DEFAULT 0,
        spend REAL NOT NULL DEFAULT 0,
        domains_json TEXT NOT NULL DEFAULT '{}',
        updated_at TEXT NOT NULL
    );
    CREATE TABLE kill_switch (
        key TEXT PRIMARY KEY,
        enabled INTEGER NOT NULL,
        reason TEXT,
        updated_at TEXT NOT NULL,
        updated_by TEXT
    );
    """,
]


# --------------------------------------------------------------------------
# connection handling
# --------------------------------------------------------------------------

def configure(path):
    """Point REACH at a database file (or ``:memory:``) and reset connections."""
    global _configured_path
    with _lock:
        _configured_path = path
        close_all()


def current_path():
    return _configured_path or config.db_path()


def close_all():
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    _local.conn = None
    _local.path = None


def connect():
    """One connection per thread, migrated on first use."""
    path = current_path()
    conn = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "path", None) == path:
        return conn
    if conn is not None:
        close_all()

    if path != ":memory:":
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)

    conn = sqlite3.connect(path, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    _local.conn = conn
    _local.path = path
    migrate(conn)
    return conn


def migrate(conn=None):
    conn = conn or connect()
    with _lock:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        for index, script in enumerate(MIGRATIONS, start=1):
            if index in applied:
                continue
            conn.executescript(script)
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, datetime('now'))",
                (index,),
            )
            conn.commit()
    return conn


def query(sql, params=()):
    return connect().execute(sql, params).fetchall()


def query_one(sql, params=()):
    return connect().execute(sql, params).fetchone()


def execute(sql, params=()):
    conn = connect()
    with _lock:
        cursor = conn.execute(sql, params)
        conn.commit()
    return cursor


def insert(table, values):
    columns = ", ".join(values)
    markers = ", ".join("?" for _ in values)
    execute(f"INSERT INTO {table} ({columns}) VALUES ({markers})", tuple(values.values()))
    return values.get("id")


def update(table, row_id, values):
    if not values:
        return
    assignments = ", ".join(f"{key} = ?" for key in values)
    execute(f"UPDATE {table} SET {assignments} WHERE id = ?", tuple(values.values()) + (row_id,))


def reset_database():
    """Drop every REACH table and re-migrate. Test-support only."""
    conn = connect()
    with _lock:
        conn.execute("PRAGMA foreign_keys = OFF")
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        for name in tables:
            conn.execute(f"DROP TABLE IF EXISTS {name}")
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
