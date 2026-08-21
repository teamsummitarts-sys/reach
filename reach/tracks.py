"""REACH's own recording catalog.

REACH is a standalone application. Nothing in this module reads another
product's database or imports another product's code: every track REACH works
with is one the rights holder entered here, stored in REACH's own ``recording``
table.

That independence has a consequence worth stating plainly. REACH is not a
royalty system and has no reporting feed, so the performance numbers a pitch may
quote — total streams, the monthly trend, which platforms report — are figures
the rights holder typed in. They are first-party in the strict sense (the artist
is the source) and they are the only performance numbers permitted into outreach
copy. Anything not entered stays UNKNOWN: nothing is inferred from a title, and
nothing is carried over from another application.

``distributed`` and ``splits_confirmed`` are deliberately tri-state. ``None``
means "the artist has not told REACH", which is not the same as ``False``, and
the release-readiness checks treat the two differently.
"""

import json
from dataclasses import dataclass, field

SAMPLE_SOURCE = "SAMPLE"


@dataclass
class Track:
    """The first-party facts REACH holds about one recording."""

    id: str
    slug: str = None
    title: str = ""
    artist_name: str = ""
    isrc: str = None
    iswc: str = None
    upc: str = None
    publisher: str = None
    writers: list = field(default_factory=list)
    producers: list = field(default_factory=list)
    alternate_titles: list = field(default_factory=list)
    distributed: bool = None
    splits_confirmed: bool = None
    streams: int = None
    monthly_streams: list = field(default_factory=list)
    reporting_platforms: list = field(default_factory=list)
    is_sample: bool = False


def _loads(value, fallback):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _tri_state(value):
    return None if value is None else bool(value)


def from_row(row):
    """Build a :class:`Track` from a ``recording`` row joined to ``artist``."""
    if row is None:
        return None
    keys = row.keys()
    return Track(
        id=row["id"],
        slug=row["slug"],
        title=row["title"],
        artist_name=row["artist_name"] if "artist_name" in keys else "",
        isrc=row["isrc"],
        iswc=row["iswc"],
        upc=row["upc"],
        publisher=row["publisher"],
        writers=_loads(row["writers_json"], []),
        producers=_loads(row["producers_json"], []),
        alternate_titles=_loads(row["alternate_titles_json"], []),
        distributed=_tri_state(row["distributed"]),
        splits_confirmed=_tri_state(row["splits_confirmed"]),
        streams=row["streams"],
        monthly_streams=_loads(row["monthly_streams_json"], []),
        reporting_platforms=_loads(row["reporting_platforms_json"], []),
        is_sample=bool(row["is_sample"]),
    )


def columns(values):
    """Map an add-track payload onto ``recording`` columns.

    Every field is optional. An absent field is stored as NULL and reads back as
    UNKNOWN — it is never defaulted to an empty string or a zero, because a zero
    stream count and an unreported stream count are different facts.
    """
    def text(key):
        raw = values.get(key)
        raw = raw.strip() if isinstance(raw, str) else raw
        return raw or None

    def listed(key):
        raw = values.get(key)
        if isinstance(raw, str):
            raw = [part.strip() for part in raw.split(",")]
        return json.dumps([item for item in (raw or []) if item])

    def integer(key):
        raw = values.get(key)
        if raw in (None, ""):
            return None
        try:
            return int(str(raw).replace(",", "").replace(" ", ""))
        except ValueError:
            return None

    def flag(key):
        raw = values.get(key)
        if raw in (None, "", "UNKNOWN"):
            return None
        if isinstance(raw, str):
            return 1 if raw.lower() in ("1", "true", "yes", "on") else 0
        return 1 if raw else 0

    return {
        "title": text("title"),
        "isrc": text("isrc"),
        "iswc": text("iswc"),
        "upc": text("upc"),
        "publisher": text("publisher"),
        "writers_json": listed("writers"),
        "producers_json": listed("producers"),
        "alternate_titles_json": listed("alternate_titles"),
        "distributed": flag("distributed"),
        "splits_confirmed": flag("splits_confirmed"),
        "streams": integer("streams"),
        "monthly_streams_json": json.dumps(integer_list(values.get("monthly_streams"))),
        "reporting_platforms_json": listed("reporting_platforms"),
    }


def integer_list(raw):
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",")]
    out = []
    for item in raw or []:
        try:
            out.append(int(str(item).replace(",", "").strip()))
        except (TypeError, ValueError):
            continue
    return out


# --------------------------------------------------------------------------
# sample catalog
# --------------------------------------------------------------------------

# A first run with an empty catalog would show nothing at all, so REACH seeds a
# small sample so the pipeline can be exercised before real tracks are entered.
# Every one of these rows is stored with is_sample = 1 and every screen that
# lists a track shows a SAMPLE badge for it: the figures below are illustrative,
# not measured, and REACH says so rather than letting them read as real
# reporting. Set REACH_SEED_SAMPLE_TRACKS=0 to start with an empty catalog, and
# delete them from the catalog screen once real tracks are in.

SAMPLE_ARTIST = "Synthwave Surfer"

SAMPLE_TRACKS = [
    {
        "slug": "midnight-drive",
        "title": "Midnight Drive",
        "isrc": "USRC12345678",
        "iswc": "T-034.524.680-1",
        "upc": "810012345671",
        "publisher": "Street Banker Publishing",
        "writers": ["Synthwave Surfer", "Jamie Rowe"],
        "producers": ["Synthwave Surfer"],
        "alternate_titles": [],
        "distributed": 1,
        "splits_confirmed": 1,
        "streams": 5_200_000,
        "monthly_streams": [412_000, 470_000, 505_000, 548_000, 590_000, 641_000],
        "reporting_platforms": ["Apple Music", "Spotify"],
    },
    {
        "slug": "neon-dreams",
        "title": "Neon Dreams",
        "isrc": "USRC12345679",
        "iswc": None,
        "upc": "810012345672",
        "publisher": "Street Banker Publishing",
        "writers": ["Synthwave Surfer"],
        "producers": ["Marco Velocity"],
        "alternate_titles": ["Neon Dreams (Extended Mix)"],
        "distributed": 1,
        "splits_confirmed": 0,
        "streams": 3_100_000,
        "monthly_streams": [240_000, 262_000, 279_000, 305_000, 318_000, 334_000],
        "reporting_platforms": ["Apple Music", "Spotify"],
    },
    {
        "slug": "city-lights",
        "title": "City Lights",
        "isrc": None,
        "iswc": None,
        "upc": None,
        "publisher": None,
        "writers": ["Synthwave Surfer", "Lila Rose"],
        "producers": [],
        "alternate_titles": [],
        "distributed": 1,
        "splits_confirmed": 0,
        "streams": 980_000,
        "monthly_streams": [128_000, 149_000, 161_000, 178_000, 164_000, 166_000],
        "reporting_platforms": ["YouTube"],
    },
    {
        "slug": "digital-paradise",
        "title": "Digital Paradise",
        "isrc": "USRC12345680",
        "iswc": "T-034.524.681-2",
        "upc": "810012345673",
        "publisher": "Street Banker Publishing",
        "writers": ["Synthwave Surfer"],
        "producers": ["Synthwave Surfer", "DJ Codec"],
        "alternate_titles": [],
        "distributed": 1,
        "splits_confirmed": 1,
        "streams": 2_500_000,
        "monthly_streams": [198_000, 214_000, 229_000, 251_000, 259_000, 267_000],
        "reporting_platforms": ["SoundCloud", "Spotify"],
    },
    {
        # Deliberately incomplete: no ISRC, no UPC, no credits, not delivered.
        # REACH must refuse to launch a campaign for it and say exactly why.
        "slug": "velvet-static",
        "title": "Velvet Static",
        "isrc": None,
        "iswc": None,
        "upc": None,
        "publisher": None,
        "writers": [],
        "producers": [],
        "alternate_titles": ["Velvet Static (Demo)"],
        "distributed": 0,
        "splits_confirmed": None,
        "streams": 210_000,
        "monthly_streams": [24_000, 29_000, 35_000, 33_000, 41_000, 48_000],
        "reporting_platforms": ["Tidal"],
    },
]


def sample_columns(entry):
    """``recording`` column values for one sample-catalog entry."""
    return {
        "title": entry["title"],
        "isrc": entry["isrc"],
        "iswc": entry["iswc"],
        "upc": entry["upc"],
        "publisher": entry["publisher"],
        "writers_json": json.dumps(entry["writers"]),
        "producers_json": json.dumps(entry["producers"]),
        "alternate_titles_json": json.dumps(entry["alternate_titles"]),
        "distributed": entry["distributed"],
        "splits_confirmed": entry["splits_confirmed"],
        "streams": entry["streams"],
        "monthly_streams_json": json.dumps(entry["monthly_streams"]),
        "reporting_platforms_json": json.dumps(entry["reporting_platforms"]),
    }
