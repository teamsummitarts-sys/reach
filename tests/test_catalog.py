"""REACH's catalog is its own.

These tests pin the behaviour that makes REACH standalone: tracks are entered
here, unentered facts stay UNKNOWN rather than becoming zeros, sample rows are
labelled as sample, and a track with campaign history cannot be deleted out from
under its evidence.
"""

import pathlib

import pytest

import reach
from reach import campaigns, catalog, db, tracks
from reach.errors import ValidationError


def reach_package_dir():
    return pathlib.Path(reach.__file__).parent


def clear_catalog():
    db.execute("DELETE FROM recording")


# --- the catalog is REACH's own ---------------------------------------------

def test_reach_imports_no_other_application():
    """The standalone bar, asserted rather than assumed.

    Every absolute import in the package must resolve to the standard library,
    a declared runtime dependency, or REACH itself. A stray ``import
    royalty_data`` — or any other product's module — fails here.
    """
    import ast
    import sys

    declared = {"flask", "jinja2", "werkzeug", "cryptography", "dns", "gunicorn"}
    allowed = set(sys.stdlib_module_names) | declared | {"reach"}

    offenders = []
    for path in sorted(pathlib.Path(reach_package_dir()).rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            offenders += [(str(path), name) for name in names if name not in allowed]
    assert offenders == [], f"REACH imports code it does not own: {offenders}"


def test_a_track_can_be_added_with_only_a_title_and_an_artist():
    recording_id = catalog.add_track({"title": "Harbour Lights", "artist_name": "Night Ferry"})
    row = catalog.get_recording(recording_id)
    assert row["title"] == "Harbour Lights"
    assert row["artist_name"] == "Night Ferry"
    assert row["slug"] == "harbour-lights"


def test_an_added_track_keeps_unentered_facts_unknown():
    recording_id = catalog.add_track({"title": "Harbour Lights", "artist_name": "Night Ferry"})
    facts = catalog.track(catalog.get_recording(recording_id))
    # Not zero, not False, not an empty string: genuinely unknown.
    assert facts.streams is None
    assert facts.distributed is None
    assert facts.splits_confirmed is None
    assert facts.isrc is None
    assert facts.writers == []


def test_entered_facts_round_trip():
    recording_id = catalog.add_track({
        "title": "Harbour Lights",
        "artist_name": "Night Ferry",
        "isrc": "GBAAA2500001",
        "publisher": "Night Ferry Music",
        "writers": "Ada Lune, Tomas Reyes",
        "producers": "Ada Lune",
        "streams": "1,240,000",
        "monthly_streams": "90000, 104000, 121000",
        "reporting_platforms": "Spotify, Bandcamp",
        "distributed": "1",
        "splits_confirmed": "0",
    })
    facts = catalog.track(catalog.get_recording(recording_id))
    assert facts.isrc == "GBAAA2500001"
    assert facts.writers == ["Ada Lune", "Tomas Reyes"]
    assert facts.streams == 1_240_000
    assert facts.monthly_streams == [90000, 104000, 121000]
    assert facts.reporting_platforms == ["Spotify", "Bandcamp"]
    assert facts.distributed is True
    # "no" is recorded as no, and is not the same value as "never answered".
    assert facts.splits_confirmed is False


def test_a_track_needs_a_title_and_an_artist():
    with pytest.raises(ValidationError):
        catalog.add_track({"artist_name": "Night Ferry"})
    with pytest.raises(ValidationError):
        catalog.add_track({"title": "Harbour Lights"})


def test_a_duplicate_identifier_is_refused():
    catalog.add_track({"title": "Harbour Lights", "artist_name": "Night Ferry"})
    with pytest.raises(ValidationError):
        catalog.add_track({"title": "Harbour Lights", "artist_name": "Night Ferry"})


# --- sample data is labelled, and optional ----------------------------------

def test_the_sample_catalog_is_marked_as_sample():
    for entry in tracks.SAMPLE_TRACKS:
        facts = catalog.track(catalog.recording_by_slug(entry["slug"]))
        assert facts.is_sample is True


def test_a_track_the_artist_adds_is_not_marked_as_sample():
    recording_id = catalog.add_track({"title": "Harbour Lights", "artist_name": "Night Ferry"})
    assert catalog.track(catalog.get_recording(recording_id)).is_sample is False


def test_sample_seeding_can_be_switched_off(monkeypatch):
    clear_catalog()
    monkeypatch.setenv(catalog.SEED_SAMPLE_ENV, "0")
    assert catalog.seed_sample_tracks() == []
    assert catalog.recordings() == []


def test_sample_seeding_never_runs_twice():
    before = len(catalog.recordings())
    assert catalog.seed_sample_tracks() == []
    assert len(catalog.recordings()) == before


# --- deletion protects evidence ---------------------------------------------

def test_an_unused_track_can_be_deleted():
    recording_id = catalog.add_track({"title": "Harbour Lights", "artist_name": "Night Ferry"})
    assert catalog.delete_track(recording_id) is True
    assert catalog.get_recording(recording_id) is None


def test_a_track_with_campaign_history_cannot_be_deleted(campaign_id, recording):
    with pytest.raises(ValidationError):
        catalog.delete_track(recording["id"])
    assert catalog.get_recording(recording["id"]) is not None
    assert campaigns.get(campaign_id) is not None


# --- readiness reads the catalog, not a guess -------------------------------

def test_release_readiness_reflects_what_was_entered():
    complete = catalog.add_track({
        "title": "Harbour Lights", "artist_name": "Night Ferry",
        "isrc": "GBAAA2500001", "upc": "5060000000001",
        "publisher": "Night Ferry Music", "writers": "Ada Lune",
        "distributed": "1", "splits_confirmed": "1",
    })
    sparse = catalog.add_track({"title": "Undertow", "artist_name": "Night Ferry"})

    complete_checks = {c["key"]: c["ok"] for c in catalog.release_readiness(complete)["checks"]}
    sparse_checks = {c["key"]: c["ok"] for c in catalog.release_readiness(sparse)["checks"]}

    for key in ("isrc", "upc", "distribution", "writers", "publisher", "splits"):
        assert complete_checks[key] is True, key
        assert sparse_checks[key] is False, key
    # Neither has a rights attestation yet, and that is the only blocking check.
    assert complete_checks["rights"] is False
    assert catalog.release_readiness(complete)["blocking"]
