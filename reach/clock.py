"""One clock, so tests can freeze time without patching a dozen modules."""

import threading
from datetime import datetime, timedelta, timezone

_state = threading.local()


def _fixed():
    return getattr(_state, "fixed", None)


def freeze(moment):
    _state.fixed = moment


def unfreeze():
    _state.fixed = None


def now():
    fixed = _fixed()
    return fixed if fixed is not None else datetime.now(timezone.utc)


def now_iso():
    return to_iso(now())


def to_iso(moment):
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse(value):
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def days_from_now(days):
    return to_iso(now() + timedelta(days=days))


def seconds_from_now(seconds):
    return to_iso(now() + timedelta(seconds=seconds))


def days_since(value):
    moment = parse(value)
    if moment is None:
        return None
    return (now() - moment).total_seconds() / 86400.0


def is_past(value):
    moment = parse(value)
    if moment is None:
        return False
    return moment <= now()
