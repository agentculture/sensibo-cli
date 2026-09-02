"""``sensibo room`` — the room naming registry (task t14).

Every sensing location Sensibo reports — the main pod and each Room Sensor
nested under it (``docs/sensibo-api.md``, "Trap 2: Room Sensor is not a
pod") — gets an operator-chosen name here. Resolution
(:func:`sensibo.store.rooms.resolve_location`) accepts the location's stable
id, its operator alias, or Sensibo's own room name, and is the hook later
verbs (``read``, ``query``, rules, the web dashboard) adopt so they display
and accept names the same way.

Two verbs:

* ``sensibo room list`` — every known location, flagging one ``STALE`` when
  it hasn't been seen inside ``--stale-after`` hours (default derived from
  :class:`sensibo.health.model.HealthConfig`'s ``down_after_seconds``, task
  t9's single source of truth for staleness).
* ``sensibo room name <location-id-or-current-name> <new-alias>`` — assign a
  persistent local alias. Dry-run by default (mandatory for every write verb
  in this project); ``--apply`` persists via :meth:`sensibo.store.Store.set_alias`,
  which never touches historical readings — they stay keyed on the stable id.
"""

from __future__ import annotations

import argparse
import datetime

from sensibo.cli._commands._automation import JSON_HELP
from sensibo.cli._commands.overview import emit_overview
from sensibo.cli._errors import EXIT_USER_ERROR, CliError
from sensibo.cli._output import emit_result
from sensibo.health.model import HealthConfig
from sensibo.store import KIND_POD, KIND_ROOM_SENSOR, LocationRecord, Store
from sensibo.store.rooms import (
    AmbiguousLocationError,
    LocationNotFoundError,
    is_stale,
    resolve_location,
)

#: The CLI/server boundary where the single source of truth for staleness
#: (task t9's ``HealthConfig.down_after_seconds``) is loaded from the
#: environment, so ``--stale-after``'s default honors an operator's
#: ``SENSIBO_HEALTH_DOWN_AFTER`` override exactly like the health evaluator
#: and the web dashboard do.
_DEFAULT_STALE_AFTER_HOURS = HealthConfig.from_env().down_after_seconds / 3600.0

# Text-only friendly labels; JSON output keeps the store's raw `kind` values
# (`pod` / `room_sensor`) so downstream consumers get a stable machine value.
_KIND_LABEL = {KIND_POD: "pod", KIND_ROOM_SENSOR: "room sensor"}

_EMPTY_STORE_REMEDIATION = "run `sensibo collect` first"


def _display_name(loc: LocationRecord) -> str:
    """The best human name for a location: alias, then Sensibo's room name, then id."""
    return loc.alias or loc.room_name or loc.id


def _iso(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).isoformat()


def _require_locations(store: Store) -> list[LocationRecord]:
    locations = store.list_locations()
    if not locations:
        raise CliError(
            code=EXIT_USER_ERROR,
            message="no known sensing locations in the store",
            remediation=_EMPTY_STORE_REMEDIATION,
        )
    return locations


def _resolve_or_raise(store: Store, name_or_id: str) -> LocationRecord:
    try:
        return resolve_location(store, name_or_id)
    except AmbiguousLocationError as err:
        candidates = ", ".join(f"{loc.id} ({_display_name(loc)})" for loc in err.candidates)
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"'{name_or_id}' matches more than one location: {candidates}",
            remediation="use the location's stable id to disambiguate",
        ) from err
    except LocationNotFoundError as err:
        known = ", ".join(err.known) if err.known else "(none)"
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"no location matches '{name_or_id}'",
            remediation=f"run `sensibo room list` to see known locations: {known}",
        ) from err


# --- room list ---------------------------------------------------------------


def _location_to_dict(loc: LocationRecord, *, stale_after_hours: float) -> dict[str, object]:
    return {
        "id": loc.id,
        "kind": loc.kind,
        "model": loc.product_model,
        "room_name": loc.room_name,
        "alias": loc.alias,
        "last_seen": loc.last_seen,
        "last_seen_iso": _iso(loc.last_seen),
        "stale": is_stale(loc.last_seen, stale_after_hours=stale_after_hours),
    }


def _render_room_list(rows: list[dict[str, object]], *, stale_after_hours: float) -> str:
    lines = [f"sensibo room list (stale after {stale_after_hours:g}h)", ""]
    for row in rows:
        kind_label = _KIND_LABEL.get(str(row["kind"]), str(row["kind"]))
        flag = "  STALE" if row["stale"] else ""
        alias = row["alias"] or "-"
        room_name = row["room_name"] or "-"
        model = row["model"] or "-"
        lines.append(
            f"{row['id']}  kind={kind_label}  model={model}  room_name={room_name}  "
            f"alias={alias}  last_seen={row['last_seen_iso']}{flag}"
        )
    return "\n".join(lines)


def cmd_room_list(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    stale_after_hours = args.stale_after
    with Store() as store:
        locations = _require_locations(store)
        rows = [_location_to_dict(loc, stale_after_hours=stale_after_hours) for loc in locations]
    if json_mode:
        emit_result(
            {"stale_after_hours": stale_after_hours, "locations": rows},
            json_mode=True,
        )
    else:
        emit_result(
            _render_room_list(rows, stale_after_hours=stale_after_hours),
            json_mode=False,
        )
    return 0


# --- room name -----------------------------------------------------------------


def cmd_room_name(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    with Store() as store:
        _require_locations(store)
        loc = _resolve_or_raise(store, args.location)
        current = _display_name(loc)
        applied = bool(args.apply)
        if applied:
            store.set_alias(loc.id, args.new_alias)
        payload = {
            "id": loc.id,
            "previous_name": current,
            "new_alias": args.new_alias,
            "applied": applied,
        }
    if json_mode:
        emit_result(payload, json_mode=True)
    elif applied:
        emit_result(
            f"renamed {loc.id} (was: {current}) to alias '{args.new_alias}'", json_mode=False
        )
    else:
        emit_result(
            f"would rename {loc.id} (currently: {current}) to alias '{args.new_alias}'\n"
            "dry-run: no changes made. Pass --apply to persist.",
            json_mode=False,
        )
    return 0


# --- overview / bare noun -------------------------------------------------------


def _room_sections() -> list[dict[str, object]]:
    return [
        {
            "title": "Verbs",
            "items": [
                "room list — every known location: id, kind, model, room name, "
                "alias, last-seen, STALE flag",
                "room name <id-or-name> <new-alias> — assign a persistent alias "
                "(dry-run by default; --apply persists)",
                "room overview — describe this noun (this command)",
            ],
        },
        {
            "title": "Name resolution",
            "items": [
                "accepts a stable id, an operator alias, or a Sensibo room name",
                "aliases win over Sensibo room names on collision",
                "ambiguous or unknown names fail with a hint: line",
                "renaming never rewrites history — readings key on the stable id only",
            ],
        },
    ]


def cmd_room_overview(args: argparse.Namespace) -> int:
    emit_overview(
        "sensibo room",
        _room_sections(),
        json_mode=bool(getattr(args, "json", False)),
    )
    return 0


def _no_verb(args: argparse.Namespace) -> int:
    # `sensibo room` with no sub-verb prints the noun's overview.
    return cmd_room_overview(args)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "room",
        help="The room naming registry (see 'sensibo room overview').",
    )
    p.add_argument("--json", action="store_true", help=JSON_HELP)
    p.set_defaults(func=_no_verb, json=False)
    # Propagate the structured-error parser class so every sub-verb's parse
    # errors route through the CliError contract, not argparse's default.
    noun_sub = p.add_subparsers(dest="room_command", parser_class=type(p))

    ov = noun_sub.add_parser("overview", help="Describe the room naming registry.")
    ov.add_argument("--json", action="store_true", help=JSON_HELP)
    ov.set_defaults(func=cmd_room_overview)

    ls = noun_sub.add_parser("list", help="List every known sensing location, flagging stale ones.")
    ls.add_argument(
        "--stale-after",
        type=float,
        default=_DEFAULT_STALE_AFTER_HOURS,
        metavar="HOURS",
        help=(
            "Flag a location STALE once it hasn't been seen for this many hours "
            f"(default: {_DEFAULT_STALE_AFTER_HOURS:g}, derived from HealthConfig's "
            "down_after_seconds; honors $SENSIBO_HEALTH_DOWN_AFTER)."
        ),
    )
    ls.add_argument("--json", action="store_true", help=JSON_HELP)
    ls.set_defaults(func=cmd_room_list)

    nm = noun_sub.add_parser(
        "name",
        help="Assign a persistent alias to a location. Dry-run by default; --apply persists.",
    )
    nm.add_argument("location", help="The location's stable id, current alias, or room name.")
    nm.add_argument("new_alias", help="The new operator-chosen alias.")
    nm.add_argument(
        "--apply",
        action="store_true",
        help="Persist the rename (default: print a dry-run preview and change nothing).",
    )
    nm.add_argument("--json", action="store_true", help=JSON_HELP)
    nm.set_defaults(func=cmd_room_name)
