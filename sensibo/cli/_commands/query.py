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
from sensibo.store import LocationRecord, ReadingRecord, Store

_COLLECT_HINT = "run 'sensibo collect' first to populate the local store"
_LOCATIONS_HINT = "list known locations with 'sensibo query locations'"


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


def _parse_iso8601(value: str, flag: str) -> float:
    """Parse an ISO 8601 timestamp into epoch seconds (UTC).

    A naive (no-tzinfo) timestamp is assumed UTC, matching
    :func:`sensibo.store.store._normalize_timestamp`. Accepts the ``Z`` UTC
    suffix (normalised to ``+00:00`` before parsing).
    """
    text = value.strip()
    if text.endswith("Z") or text.endswith("z"):
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
