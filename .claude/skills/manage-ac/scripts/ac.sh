#!/usr/bin/env bash
# One-shot AC control via the installed `sensibo` CLI.
#
# Usage: bash ac.sh <verb> [args]
#
# Verbs:
#   status                        Fleet (live) + latest stored readings (offline)
#   read [location]               Live snapshot (pod id or ms_* Room Sensor id)
#   on [pod|all]                  Power on
#   off [pod|all]                 Power off
#   set <temp> [pod|all]          Target temperature
#   mode <cool|heat|fan|dry|auto> [pod|all]
#   fan <level> [pod|all]         Device-specific (quiet/low/medium/high/auto)
#
# The pod argument may be omitted when the fleet has exactly one pod (it is
# resolved from `sensibo devices --json`); `all` targets the whole fleet.
#
# One-shot by design: control verbs run `sensibo set ... --apply` so a single
# call changes the AC — this wrapper exists for explicit control actions.
# Pass --dry-run to preview without committing. The raw CLI stays dry-run by
# default; do not add a --no-apply flag here (see SKILL.md red flags).
#
# API budget: `status` makes exactly one fleet API call (readings come from
# the offline local store); `read` and each control verb make one fleet call.
# Never use this in a polling loop — that is what `sensibo collect` /
# `sensibo rule` are for.

set -euo pipefail

DRY_RUN=""
JSON_MODE=""
SENSIBO="${SENSIBO:-sensibo}"

usage() {
    cat <<'EOF'
Usage: ac.sh <verb> [args]

  status                        Fleet (live) + latest stored readings (offline)
  read [location]               Live snapshot (pod id or ms_* Room Sensor id)
  on [pod|all]                  Power on
  off [pod|all]                 Power off
  set <temp> [pod|all]          Target temperature
  mode <cool|heat|fan|dry|auto> [pod|all]
  fan <level> [pod|all]         Device-specific (quiet/low/medium/high/auto)

Flags:
  --json      Machine-readable output (all verbs; status: a single JSON doc)
  --dry-run   Preview the change without committing (control verbs only)

Omitted pod argument resolves to the fleet's single pod; `all` targets
every pod. Control verbs commit by design (one-shot); --dry-run previews.
EOF
}

fail() {
    echo "ERROR: $*" >&2
    usage >&2
    exit 1
}

# Populate the global PODS array from one live fleet call.
# Exits 2 (environment error) on discovery or parse failure — a failed
# `sensibo devices` must not masquerade as an empty fleet.
resolve_pods() {
    local doc pod_ids line
    doc="$("$SENSIBO" devices --json)" || {
        echo "ERROR: fleet discovery failed ('$SENSIBO devices --json' exited non-zero; see sensibo error above)" >&2
        exit 2
    }
    pod_ids="$(printf '%s' "$doc" | python3 -c '
import json, sys
doc = json.load(sys.stdin)
for d in doc.get("devices", []):
    if d.get("kind") == "pod":
        print(d["id"])
')" || {
        echo "ERROR: failed to parse fleet JSON from '$SENSIBO devices --json'" >&2
        exit 2
    }
    PODS=()
    while IFS= read -r line; do
        if [[ -n "$line" ]]; then
            PODS+=("$line")
        fi
    done <<< "$pod_ids"
}

# $1 = explicit target ("" or "all" or a pod id). Prints the resolved target.
# Requires resolve_pods to have run.
resolve_target() {
    local target="${1:-}"
    if [[ -n "$target" ]]; then
        printf '%s\n' "$target"
        return
    fi
    if [[ ${#PODS[@]} -eq 1 ]]; then
        printf '%s\n' "${PODS[0]}"
    elif [[ ${#PODS[@]} -eq 0 ]]; then
        echo "ERROR: no pods in the fleet" >&2
        exit 2
    else
        echo "ERROR: fleet has ${#PODS[@]} pods; name one explicitly: ${PODS[*]}" >&2
        exit 2
    fi
}

do_status() {
    local json_flag=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --json)  json_flag=1; shift ;;
            -h|--help) usage; exit 0 ;;
            *) fail "unknown argument '$1' for status" ;;
        esac
    done

    if [[ -n "$json_flag" ]]; then
        # One live fleet call + one offline store read, merged into a single
        # well-formed JSON document on stdout.
        local fleet_json latest_json store_state
        fleet_json="$("$SENSIBO" devices --json)" || {
            echo "ERROR: fleet discovery failed ('$SENSIBO devices --json' exited non-zero; see sensibo error above)" >&2
            exit 2
        }
        if latest_json="$("$SENSIBO" query latest --json 2>/dev/null)"; then
            store_state="ok"
        else
            latest_json=""
            store_state="empty"
        fi
        python3 -c '
import json, sys
fleet = json.loads(sys.argv[1])
latest = json.loads(sys.argv[2]) if sys.argv[2] else None
print(json.dumps({"fleet": fleet, "latest": latest, "store": sys.argv[3]}, indent=2))
' "$fleet_json" "$latest_json" "$store_state"
    else
        local latest_text
        "$SENSIBO" devices || exit 2
        echo
        if latest_text="$("$SENSIBO" query latest 2>/dev/null)"; then
            printf '%s\n' "$latest_text"
        else
            echo "(no local store readings — 'sensibo collect' populates the offline store; live snapshot: 'sensibo read <location>')"
        fi
    fi
}

do_read() {
    local loc=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --json)    JSON_MODE=1; shift ;;
            -h|--help) usage; exit 0 ;;
            -*) fail "unknown flag '$1' for read" ;;
            *)
                if [[ -n "$loc" ]]; then
                    fail "multiple locations ('$loc' and '$1'); name exactly one"
                fi
                loc="$1"; shift
                ;;
        esac
    done

    resolve_pods
    loc="$(resolve_target "$loc")"

    local cmd=("$SENSIBO" read "$loc")
    if [[ -n "$JSON_MODE" ]]; then
        cmd+=(--json)
    fi
    echo "Running: ${cmd[*]}" >&2
    exec "${cmd[@]}"
}

dispatch_control() {
    local verb="$1"; shift
    local extra=()
    case "$verb" in
        set)
            if [[ $# -lt 1 ]]; then
                fail "'set' requires a temperature (e.g. ac.sh set 22)"
            fi
            extra+=(--target "$1"); shift ;;
        mode)
            if [[ $# -lt 1 ]]; then
                fail "'mode' requires one of cool|heat|fan|dry|auto (e.g. ac.sh mode cool)"
            fi
            extra+=(--mode "$1"); shift ;;
        fan)
            if [[ $# -lt 1 ]]; then
                fail "'fan' requires a level (e.g. ac.sh fan auto)"
            fi
            extra+=(--fan "$1"); shift ;;
    esac

    local target=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --dry-run) DRY_RUN=1; shift ;;
            --json)    JSON_MODE=1; shift ;;
            -h|--help) usage; exit 0 ;;
            -*) fail "unknown flag '$1'" ;;
            *)
                if [[ -n "$target" ]]; then
                    fail "multiple targets ('$target' and '$1'); name exactly one pod or 'all'"
                fi
                target="$1"; shift
                ;;
        esac
    done

    resolve_pods
    local t
    t="$(resolve_target "$target")"

    local cmd=("$SENSIBO" set)
    if [[ "$t" == "all" ]]; then
        cmd+=(--all)
    else
        cmd+=("$t")
    fi
    case "$verb" in
        on)  cmd+=(--power on) ;;
        off) cmd+=(--power off) ;;
    esac
    cmd+=("${extra[@]}")
    if [[ -n "$JSON_MODE" ]]; then
        cmd+=(--json)
    fi
    if [[ -z "$DRY_RUN" ]]; then
        cmd+=(--apply)
    fi

    echo "Running: ${cmd[*]}" >&2
    exec "${cmd[@]}"
}

main() {
    local verb="${1:-}"
    if [[ -z "$verb" ]]; then
        usage
        exit 1
    fi
    shift
    case "$verb" in
        -h|--help)
            usage
            ;;
        status)
            do_status "$@"
            ;;
        read)
            do_read "$@"
            ;;
        on|off|set|mode|fan)
            dispatch_control "$verb" "$@"
            ;;
        *)
            echo "ERROR: unknown verb '$verb'" >&2
            usage >&2
            exit 1
            ;;
    esac
}

main "$@"
