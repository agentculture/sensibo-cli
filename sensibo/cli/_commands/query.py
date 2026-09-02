"""``sensibo query`` — offline reads from the local store (task t7).

This noun **never touches the network**. It answers exclusively from the
local sqlite store (:mod:`sensibo.store`) — that is the product's offline-
query promise, and the reason ``sensibo/store/`` has no network imports at
all (see ``tests/test_store.py``'s socket-blocking guard, mirrored here).

Three verbs:

- ``query latest [<location-id>] [--field <name>]`` — the latest value(s)
  per location/field.
- ``query range <location-id> --field <name> [--since ISO] [--until ISO]`` —
  time-series rows; ``--since``/``--until`` are **inclusive** on both ends
  (matches :meth:`sensibo.store.Store.query_range`).
- ``query locations`` — every known sensing location with its metadata.

An empty store (nothing collected yet) or a location id the store has never
seen both raise :class:`~sensibo.cli._errors.CliError` pointing the operator
at ``sensibo collect`` — that verb is what populates the store this command
reads from.

``--db`` (else ``SENSIBO_DB``, else ``~/.sensibo/sensibo.db``) is honored via
:class:`sensibo.store.Store`'s own path resolution — this module never
resolves a path itself.
"""

from __future__ import annotations

import argparse
import datetime

from sensibo.cli._errors import EXIT_USER_ERROR, CliError
from sensibo.cli._output import emit_result
from sensibo.health import EXECUTION_LOCAL, STATUS_DOWN, STATUS_OK, iso8601
from sensibo.store import (
    HealthRecord,
    LocationRecord,
    ReadingRecord,
    Store,
    StoreVersionError,
    TransitionRecord,
)
from sensibo.store.rooms import (
    AmbiguousLocationError,
    LocationNotFoundError,
    resolve_location,
)

_COLLECT_HINT = "run 'sensibo collect' first to populate the local store"
_LOCATIONS_HINT = "list known locations with 'sensibo query locations'"

#: The field name every local-execution payload carries (matches
#: sensibo/rules/model.py and sensibo/cli/_cloud.py's own EXECUTION_FIELD).
EXECUTION_FIELD = "execution"


# -- error helpers ------------------------------------------------------------


def _empty_store_error() -> CliError:
    return CliError(
        code=EXIT_USER_ERROR,
        message="the local store has no data yet",
        remediation=_COLLECT_HINT,
    )


def _unknown_location_error(location_id: str) -> CliError:
    return CliError(
        code=EXIT_USER_ERROR,
        message=f"unknown location: {location_id!r}",
        remediation=f"{_LOCATIONS_HINT}; {_COLLECT_HINT}",
    )


def _empty_health_error() -> CliError:
    return CliError(
        code=EXIT_USER_ERROR,
        message="the local store has no sensor health data yet",
        remediation=_COLLECT_HINT,
    )


def _no_health_for_location_error(location_id: str) -> CliError:
    return CliError(
        code=EXIT_USER_ERROR,
        message=f"no health data recorded yet for location: {location_id!r}",
        remediation=_COLLECT_HINT,
    )


def _store_version_error(err: StoreVersionError) -> CliError:
    return CliError(
        code=2,
        message=str(err),
        remediation=err.remediation,
    )


def _resolve_location_or_raise(store: Store, name_or_id: str) -> LocationRecord:
    try:
        return resolve_location(store, name_or_id)
    except AmbiguousLocationError as err:
        candidates = ", ".join(loc.id for loc in err.candidates)
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"'{name_or_id}' matches more than one location: {candidates}",
            remediation="use the location's stable id to disambiguate",
        ) from err
    except LocationNotFoundError as err:
        known = ", ".join(err.known) if err.known else "(none)"
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"no location matches {name_or_id!r}",
            remediation=f"{_LOCATIONS_HINT}: {known}",
        ) from err


def _parse_iso8601(value: str, flag: str) -> float:
    """Parse an ISO 8601 timestamp into epoch seconds (UTC).

    A naive (no-tzinfo) timestamp is assumed UTC, matching
    :func:`sensibo.store.store._normalize_timestamp`. Accepts the ``Z`` UTC
    suffix (normalised to ``+00:00`` before parsing).
    """
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(text)
    except ValueError as err:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"invalid {flag} timestamp: {value!r} ({err})",
            remediation=(f"pass {flag} as ISO 8601, e.g. 2026-07-01 or 2026-07-01T00:00:00Z"),
        ) from err
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def _format_iso(ts: float) -> str:
    return (
        datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


# -- reading / location wire shapes -------------------------------------------


def _reading_to_dict(r: ReadingRecord) -> dict[str, object]:
    return {
        "location_id": r.location_id,
        "field": r.field,
        "timestamp": r.timestamp,
        "timestamp_iso": _format_iso(r.timestamp),
        "value": r.value,
        "unit": r.unit,
    }


def _location_to_dict(loc: LocationRecord) -> dict[str, object]:
    return {
        "id": loc.id,
        "kind": loc.kind,
        "product_model": loc.product_model,
        "parent_pod_id": loc.parent_pod_id,
        "room_name": loc.room_name,
        "alias": loc.alias,
        "first_seen": loc.first_seen,
        "first_seen_iso": _format_iso(loc.first_seen),
        "last_seen": loc.last_seen,
        "last_seen_iso": _format_iso(loc.last_seen),
    }


def _render_value(value: float | str) -> str:
    return value if isinstance(value, str) else f"{value:g}"


def _render_readings_table(readings: list[ReadingRecord]) -> str:
    if not readings:
        return "(no matching readings)"
    header = f"{'location':<24} {'field':<20} {'value':>12} {'unit':<8} timestamp"
    lines = [header]
    for r in readings:
        lines.append(
            f"{r.location_id:<24} {r.field:<20} {_render_value(r.value):>12} "
            f"{(r.unit or ''):<8} {_format_iso(r.timestamp)}"
        )
    return "\n".join(lines)


def _render_locations_table(locations: list[LocationRecord]) -> str:
    header = f"{'id':<24} {'kind':<12} {'model':<10} {'room':<16} {'alias':<16} last-seen"
    lines = [header]
    for loc in locations:
        lines.append(
            f"{loc.id:<24} {loc.kind:<12} {(loc.product_model or ''):<10} "
            f"{(loc.room_name or ''):<16} {(loc.alias or ''):<16} {_format_iso(loc.last_seen)}"
        )
    return "\n".join(lines)


# -- health wire shape ---------------------------------------------------------


def _transition_to_dict(t: TransitionRecord) -> dict[str, object]:
    return {
        "from_status": t.from_status,
        "to_status": t.to_status,
        "at": iso8601(t.at),
    }


def _compute_outages(transitions: list[TransitionRecord]) -> list[dict[str, object]]:
    """Closed down->ok pairs, oldest first, each with a computed duration.

    ``transitions`` must already be oldest-first (matches
    :meth:`sensibo.store.Store.list_transitions`). A ``down`` still without a
    matching ``ok`` (an open outage) contributes nothing here — only closed
    pairs get a duration.
    """
    outages: list[dict[str, object]] = []
    pending_down: TransitionRecord | None = None
    for t in transitions:
        if t.to_status == STATUS_DOWN:
            pending_down = t
        elif t.to_status == STATUS_OK and pending_down is not None:
            outages.append(
                {
                    "start": iso8601(pending_down.at),
                    "end": iso8601(t.at),
                    "duration_seconds": t.at - pending_down.at,
                }
            )
            pending_down = None
    return outages


def _health_to_dict(h: HealthRecord, transitions: list[TransitionRecord]) -> dict[str, object]:
    return {
        "location_id": h.location_id,
        "status": h.status,
        "since": iso8601(h.since),
        "last_ok": iso8601(h.last_ok) if h.last_ok is not None else "never",
        "parent_pod_id": h.parent_pod_id,
        "transitions": [_transition_to_dict(t) for t in transitions],
        "outages": _compute_outages(transitions),
    }


def _render_health_text(payload: dict[str, object]) -> str:
    collector: dict[str, object] = payload["collector"]  # type: ignore[assignment]
    last_cycle_at = collector["last_cycle_at"] or "never"
    last_cycle_outcome = collector["last_cycle_outcome"] or "unknown"
    lines = [
        f"{EXECUTION_FIELD}: {payload[EXECUTION_FIELD]}",
        "",
        f"collector last_cycle_at: {last_cycle_at}",
        f"collector last_cycle_outcome: {last_cycle_outcome}",
        "",
    ]
    locations = payload["locations"]
    if not locations:
        lines.append("(no health data for the requested location)")
        return "\n".join(lines)
    for loc in locations:  # type: ignore[assignment]
        lines.append(f"location: {loc['location_id']}")
        lines.append(f"  status: {loc['status']}")
        lines.append(f"  since: {loc['since']}")
        lines.append(f"  last_ok: {loc['last_ok']}")
        if loc["outages"]:
            lines.append("  outages:")
            for o in loc["outages"]:
                lines.append(
                    f"    start={o['start']} end={o['end']} "
                    f"duration_seconds={o['duration_seconds']:g}"
                )
        if loc["transitions"]:
            lines.append("  transitions:")
            for t in loc["transitions"]:
                lines.append(f"    {t['from_status']} -> {t['to_status']} at {t['at']}")
        lines.append("")
    return "\n".join(lines).rstrip("\n")


# -- verb handlers -------------------------------------------------------------


def cmd_query_latest(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    with Store(db_path=args.db) as store:
        locations = store.list_locations()
        if not locations:
            raise _empty_store_error()

        if args.location is not None:
            loc = store.get_location(args.location)
            if loc is None:
                raise _unknown_location_error(args.location)
            target_locations = [loc]
        else:
            target_locations = locations

        readings: list[ReadingRecord] = []
        for loc in target_locations:
            if args.field:
                reading = store.latest_reading(loc.id, args.field)
                if reading is not None:
                    readings.append(reading)
            else:
                readings.extend(store.latest_readings(loc.id).values())

    readings.sort(key=lambda r: (r.location_id, r.field))
    if json_mode:
        emit_result({"readings": [_reading_to_dict(r) for r in readings]}, json_mode=True)
    else:
        emit_result(_render_readings_table(readings), json_mode=False)
    return 0


def cmd_query_range(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    since_ts = _parse_iso8601(args.since, "--since") if args.since else None
    until_ts = _parse_iso8601(args.until, "--until") if args.until else None

    with Store(db_path=args.db) as store:
        loc = store.get_location(args.location)
        if loc is None:
            raise _unknown_location_error(args.location)
        rows = store.query_range(loc.id, args.field, since=since_ts, until=until_ts)

    if json_mode:
        payload = {
            "location_id": args.location,
            "field": args.field,
            "since": args.since,
            "until": args.until,
            "readings": [_reading_to_dict(r) for r in rows],
        }
        emit_result(payload, json_mode=True)
    else:
        header = f"# {args.location} / {args.field}"
        emit_result(f"{header}\n{_render_readings_table(rows)}", json_mode=False)
    return 0


def cmd_query_locations(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    with Store(db_path=args.db) as store:
        locations = store.list_locations()

    if not locations:
        raise _empty_store_error()

    if json_mode:
        emit_result({"locations": [_location_to_dict(loc) for loc in locations]}, json_mode=True)
    else:
        emit_result(_render_locations_table(locations), json_mode=False)
    return 0


def cmd_query_health(args: argparse.Namespace) -> int:
    """``sensibo query health [LOCATION] [--since ISO] [--json]`` — offline only.

    Answers exclusively from :meth:`Store.list_health` /
    :meth:`Store.list_transitions` / :meth:`Store.get_meta` — never the
    network, same promise as every other ``query`` verb. Sensor health rows
    are populated by ``sensibo collect``'s cycle-by-cycle evaluation (a
    sibling task); an empty ``health`` table means no cycle has run yet.
    """
    json_mode = bool(getattr(args, "json", False))
    since_ts = _parse_iso8601(args.since, "--since") if args.since else None

    try:
        with Store(db_path=args.db) as store:
            health_rows = store.list_health()
            if not health_rows:
                raise _empty_health_error()

            by_id = {h.location_id: h for h in health_rows}
            if args.location is not None:
                loc = _resolve_location_or_raise(store, args.location)
                if loc.id not in by_id:
                    raise _no_health_for_location_error(loc.id)
                target_ids = [loc.id]
            else:
                target_ids = sorted(by_id)

            locations_out = []
            for lid in target_ids:
                transitions = store.list_transitions(location_id=lid, since=since_ts)
                locations_out.append(_health_to_dict(by_id[lid], transitions))

            last_cycle_at = store.get_meta("last_cycle_at")
            last_cycle_outcome = store.get_meta("last_cycle_outcome")
    except StoreVersionError as err:
        raise _store_version_error(err) from err

    payload: dict[str, object] = {
        "locations": locations_out,
        "collector": {"last_cycle_at": last_cycle_at, "last_cycle_outcome": last_cycle_outcome},
        EXECUTION_FIELD: EXECUTION_LOCAL,
    }
    if json_mode:
        emit_result(payload, json_mode=True)
    else:
        emit_result(_render_health_text(payload), json_mode=False)
    return 0


# -- registration ---------------------------------------------------------


def _add_db_and_json(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="Override the store path (else SENSIBO_DB, else ~/.sensibo/sensibo.db).",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "query",
        help=(
            "Offline reads from the local store (latest, range, locations); "
            "never touches the network."
        ),
    )

    def _no_verb(_args: argparse.Namespace) -> int:
        p.print_help()
        return 0

    p.set_defaults(func=_no_verb)
    # `p` is a _CliArgumentParser (propagated from the top-level subparsers'
    # parser_class), so nested subparsers built with type(p) route their own
    # parse errors through the structured error contract too.
    query_sub = p.add_subparsers(dest="query_command", parser_class=type(p))

    latest_p = query_sub.add_parser("latest", help="Latest reading(s) per location/field.")
    latest_p.add_argument(
        "location",
        nargs="?",
        default=None,
        help="Location id to restrict to (default: every known location).",
    )
    latest_p.add_argument(
        "--field", default=None, help="Field to restrict to (default: every field)."
    )
    _add_db_and_json(latest_p)
    latest_p.set_defaults(func=cmd_query_latest)

    range_p = query_sub.add_parser("range", help="Time-series rows for one field at one location.")
    range_p.add_argument("location", help="Location id (see 'sensibo query locations').")
    range_p.add_argument("--field", required=True, help="Field to read.")
    range_p.add_argument("--since", default=None, metavar="ISO8601", help="Inclusive lower bound.")
    range_p.add_argument("--until", default=None, metavar="ISO8601", help="Inclusive upper bound.")
    _add_db_and_json(range_p)
    range_p.set_defaults(func=cmd_query_range)

    locations_p = query_sub.add_parser(
        "locations", help="Every known sensing location with its metadata."
    )
    _add_db_and_json(locations_p)
    locations_p.set_defaults(func=cmd_query_locations)

    health_p = query_sub.add_parser(
        "health",
        help=(
            "Sensor health: current status, since, last_ok, and outage "
            "(transition) history per location."
        ),
    )
    health_p.add_argument(
        "location",
        nargs="?",
        default=None,
        help="Location id, alias, or room name to restrict to (default: every location).",
    )
    health_p.add_argument(
        "--since",
        default=None,
        metavar="ISO8601",
        help="Only include transitions at/after this time.",
    )
    _add_db_and_json(health_p)
    health_p.set_defaults(func=cmd_query_health)
