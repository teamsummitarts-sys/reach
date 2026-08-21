"""Track Intelligence Profile.

The profile is the input to query planning and to scoring. Every field carries
value, confidence, source, extractor version, timestamp and human-override
state — so the campaign can always answer "where did this come from?".

Fields REACH cannot derive stay UNKNOWN with confidence 0. They are never
guessed, and the campaign wizard asks the user for the ones that matter.

Deliberately absent: Spotify Audio Features, Spotify Audio Analysis and Spotify
Related Artists. Nothing here depends on them.
"""

import json

from . import audit, catalog, clock, db, rbac, firstparty
from .errors import ValidationError

EXTRACTOR_VERSION = "profile/1.0.0"

SOURCE_REACH_CATALOG = "reach_catalog"
SOURCE_USER = "user_input"
SOURCE_MUSICBRAINZ = "musicbrainz"
SOURCE_UNKNOWN = "UNKNOWN"

TEXT = "text"
LIST = "list"
NUMBER = "number"
BOOL = "bool"

# field, label, kind, required_for_launch, help
FIELD_SPEC = [
    ("primary_genre", "Primary genre", TEXT, True, "Drives every discovery query family."),
    ("secondary_genres", "Secondary genres", LIST, False, "Broadens query families."),
    ("microgenres", "Microgenres", LIST, True, "The strongest relevance signal REACH has."),
    ("bpm", "BPM", NUMBER, False, "Used for DJ and club-pool relevance."),
    ("musical_key", "Musical key", TEXT, False, "Used by some DJ pools."),
    ("time_signature", "Time signature", TEXT, False, ""),
    ("duration_seconds", "Duration", NUMBER, False, "Radio formats have length limits."),
    ("energy", "Energy", TEXT, False, "Low / medium / high."),
    ("mood", "Mood", LIST, True, "Playlist context matching."),
    ("danceability", "Danceability estimate", TEXT, False, ""),
    ("vocal_style", "Vocal style", TEXT, False, ""),
    ("vocal_range", "Vocal range description", TEXT, False, ""),
    ("vocal_presentation", "Vocal presentation", TEXT, False,
     "User-supplied only, and only when relevant to a submission requirement."),
    ("instrumentation", "Instrumentation", LIST, False, ""),
    ("production_style", "Production style", TEXT, False, ""),
    ("era_influences", "Era influences", LIST, False, ""),
    ("cultural_scene", "Cultural scene", TEXT, False, "Scene-specific query families."),
    ("lyrical_themes", "Lyrical themes", LIST, False, ""),
    ("explicit", "Explicit", BOOL, False, "Radio and editorial eligibility."),
    ("language", "Language", TEXT, True, "Controls outreach language selection."),
    ("comparable_artists", "Comparable artists", LIST, True,
     "Used verbatim in queries and in the pitch's relevance sentence."),
    ("target_audiences", "Target audiences", LIST, False, ""),
    ("playlist_contexts", "Likely playlist contexts", LIST, False, ""),
    ("radio_formats", "Likely radio formats", LIST, False, ""),
    ("geographic_affinity", "Geographic affinity", LIST, False, ""),
    ("live_relevance", "Live-show relevance", TEXT, False, ""),
    ("visual_identity", "Visual identity", TEXT, False, ""),
    ("release_narrative", "Release narrative", TEXT, False, "Feeds the pitch context line."),
    ("marketing_angle", "Marketing angle", TEXT, False, ""),
]

FIELDS_BY_NAME = {name: (label, kind, required, help_text)
                  for name, label, kind, required, help_text in FIELD_SPEC}
REQUIRED_FIELDS = [name for name, _, _, required, _ in FIELD_SPEC if required]


def _coerce(kind, value):
    if value is None or value == "":
        return None
    if kind == LIST:
        if isinstance(value, list):
            items = value
        else:
            items = [part.strip() for part in str(value).split(",")]
        items = [item for item in items if item]
        return items or None
    if kind == NUMBER:
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValidationError(f"Expected a number, got {value!r}")
        return int(number) if number.is_integer() else number
    if kind == BOOL:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on", "explicit")
    return str(value).strip()


def get_or_create(recording_id):
    principal = rbac.current_principal()
    row = db.query_one(
        "SELECT * FROM track_profile WHERE recording_id = ? ORDER BY version DESC LIMIT 1",
        (recording_id,),
    )
    if row is not None:
        return row["id"]
    profile_id = db.new_id("prof")
    now = clock.now_iso()
    db.insert("track_profile", {
        "id": profile_id,
        "tenant_id": principal.tenant_id,
        "recording_id": recording_id,
        "version": 1,
        "created_at": now,
        "updated_at": now,
    })
    _seed_derived_fields(profile_id, recording_id)
    audit.record("profile.created", entity_type="track_profile", entity_id=profile_id,
                 payload={"recording_id": recording_id})
    return profile_id


def _seed_derived_fields(profile_id, recording_id):
    """Only what REACH's own catalog actually knows. Nothing is invented."""
    recording = catalog.get_recording(recording_id)
    facts = catalog.track(recording)
    if recording is None:
        return

    if recording["explicit"] is not None:
        _write(profile_id, "explicit", bool(recording["explicit"]), 1.0, SOURCE_REACH_CATALOG)
    if recording["duration_seconds"]:
        _write(profile_id, "duration_seconds", recording["duration_seconds"], 1.0,
               SOURCE_REACH_CATALOG)
    if facts is not None:
        derived = firstparty.derived_profile_hints(facts)
        for field, (value, confidence) in derived.items():
            if value:
                _write(profile_id, field, value, confidence, SOURCE_REACH_CATALOG)


def _write(profile_id, field, value, confidence, source, human_override=False):
    existing = db.query_one(
        "SELECT id, human_override FROM track_profile_field WHERE profile_id = ? AND field = ?",
        (profile_id, field),
    )
    now = clock.now_iso()
    payload = {
        "value_json": json.dumps(value),
        "confidence": confidence,
        "source": source,
        "extractor_version": EXTRACTOR_VERSION,
        "generated_at": now,
        "human_override": 1 if human_override else 0,
    }
    if existing is not None:
        # A human override is never silently replaced by a derived value.
        if existing["human_override"] and not human_override:
            return existing["id"]
        db.update("track_profile_field", existing["id"], payload)
        return existing["id"]
    field_id = db.new_id("pf")
    payload.update({"id": field_id, "profile_id": profile_id, "field": field})
    db.insert("track_profile_field", payload)
    return field_id


def set_field(profile_id, field, value, source=SOURCE_USER, confidence=None):
    if field not in FIELDS_BY_NAME:
        raise ValidationError(f"Unknown profile field: {field}")
    _, kind, _, _ = FIELDS_BY_NAME[field]
    coerced = _coerce(kind, value)
    human = source == SOURCE_USER
    if confidence is None:
        confidence = 1.0 if human else 0.6
    if coerced is None:
        db.execute(
            "DELETE FROM track_profile_field WHERE profile_id = ? AND field = ?",
            (profile_id, field),
        )
        return None
    field_id = _write(profile_id, field, coerced, confidence, source, human_override=human)
    db.update("track_profile", profile_id, {"updated_at": clock.now_iso()})
    return field_id


def fields(profile_id):
    rows = db.query(
        "SELECT * FROM track_profile_field WHERE profile_id = ?", (profile_id,)
    )
    by_field = {row["field"]: row for row in rows}
    result = []
    for name, label, kind, required, help_text in FIELD_SPEC:
        row = by_field.get(name)
        if row is None:
            result.append({
                "field": name, "label": label, "kind": kind, "required": required,
                "help": help_text, "value": None, "display": "UNKNOWN", "confidence": 0.0,
                "source": SOURCE_UNKNOWN, "extractor_version": None,
                "generated_at": None, "human_override": False, "known": False,
            })
            continue
        value = json.loads(row["value_json"]) if row["value_json"] else None
        result.append({
            "field": name, "label": label, "kind": kind, "required": required,
            "help": help_text, "value": value, "display": _display(value),
            "confidence": row["confidence"], "source": row["source"],
            "extractor_version": row["extractor_version"],
            "generated_at": row["generated_at"],
            "human_override": bool(row["human_override"]), "known": True,
        })
    return result


def _display(value):
    if value is None:
        return "UNKNOWN"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


def values(profile_id):
    """Flat {field: value} for engines that just need the data."""
    return {
        item["field"]: item["value"]
        for item in fields(profile_id)
        if item["value"] is not None
    }


def completeness(profile_id):
    items = fields(profile_id)
    known = [item for item in items if item["known"]]
    missing_required = [item["field"] for item in items if item["required"] and not item["known"]]
    return {
        "known": len(known),
        "total": len(items),
        "score": round(len(known) / len(items) * 100) if items else 0,
        "missing_required": missing_required,
        "ready": not missing_required,
    }


def profile_for_recording(recording_id):
    profile_id = get_or_create(recording_id)
    return {
        "id": profile_id,
        "fields": fields(profile_id),
        "values": values(profile_id),
        "completeness": completeness(profile_id),
    }
