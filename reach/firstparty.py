"""First-party facts about a recording — the only facts allowed into pitch copy.

Everything here comes from REACH's own catalog: values the rights holder entered
about their own release. That is why the firewall permits this source into a
draft when it permits almost nothing else — there is no third party to
misattribute and no licensed feed to redistribute.

Note what this module deliberately does *not* do: it does not invent musical
attributes. REACH's catalog models identity, credits and reported performance —
not BPM, key or genre. So :func:`derived_profile_hints` returns only what is
genuinely on the record, and the campaign wizard asks the artist for the rest
rather than guessing. This function is also the seam where a distributor feed,
a DDEX RIN import or local audio analysis would attach later.
"""

AUDIO_ANALYSIS_STATE = "NO_AUDIO_SOURCE_CONFIGURED"


def derived_profile_hints(track):
    """{field: (value, confidence)} for fields the catalog really contains."""
    hints = {}
    # REACH's catalog has no musical attributes to derive. Anything added here
    # must come from a real stored field, never from a title or an artist name.
    if track is None:
        return hints
    if track.alternate_titles:
        # Alternate titles are a genuine catalog field and are useful when a
        # curator's guidelines ask which version is being submitted.
        hints["release_narrative"] = (
            f"Also catalogued as: {', '.join(track.alternate_titles)}",
            0.5,
        )
    return hints


def performance_baseline(track):
    """First-party performance facts. Safe to quote in outreach.

    Every number here was entered by the rights holder. REACH has no reporting
    feed of its own, so an absent figure stays absent — it is never estimated
    from the numbers that are present.
    """
    if track is None:
        return {}
    trend = list(track.monthly_streams or [])
    growth = None
    if len(trend) >= 2 and trend[0] > 0:
        growth = round((trend[-1] - trend[0]) / trend[0] * 100)
    return {
        "streams": track.streams,
        "monthly_streams": trend,
        "trend_growth_pct": growth,
        "reporting_platforms": list(track.reporting_platforms or []),
        "writers": list(track.writers or []),
        "producers": list(track.producers or []),
        "publisher": track.publisher,
        "isrc": track.isrc,
    }


def audio_analysis_state():
    """Local deterministic audio analysis is unavailable until audio files are
    attached to a recording. Reported honestly rather than simulated."""
    return {
        "state": AUDIO_ANALYSIS_STATE,
        "detail": "No audio file is attached to this recording, so BPM, key and energy "
                  "cannot be computed locally. These stay user-supplied.",
    }
