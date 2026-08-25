#!/usr/bin/env bash
# One-shot AC control via the installed `sensibo` CLI.
#
# Usage: bash ac.sh <verb> [args]
#
# Verbs:
#   status                        Fleet + one live snapshot per pod
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

set -euo pipefail

DRY_RUN=""
SENSIBO="${SENSIBO:-sensibo}"

usage() {
    cat <<'EOF'
Usage: ac.sh <verb> [args]

  status                        Fleet + one live snapshot per pod
  read [location]               Live snapshot (pod id or ms_* Room Sensor id)
  on [pod|all]                  Power on
  off [pod|all]                 Power off
  set <temp> [pod|all]          Target temperature
  mode <cool|heat|fan|dry|auto> [pod|all]
  fan <level> [pod|all]         Device-specific (quiet/low/medium/high/auto)

Flags:
  --dry-run   Preview the change without committing (control verbs only)

Omitted pod argument resolves to the fleet's single pod; `all` targets
every pod. Control verbs commit by design (one-shot); --dry-run previews.
EOF
}

# Print pod ids, one per line, from the live fleet listing.
resolve_pods() {
    "$SENSIBO" devices --json | python3 -c '
import json, sys
doc = json.load(sys.stdin)
for d in doc.get("devices", []):
    if d.get("kind") == "pod":
        print(d["id"])
'
}

# $1 = explicit target ("" or "all" or a pod id) -> resolved target or "all"
resolve_target() {
    local target="${1:-}"
    if [[ -n "$target" ]]; then
        printf '%s\n' "$target"
        return
    fi
    local pods=()
    mapfile -t pods < <(resolve_pods)
    if [[ ${#pods[@]} -eq 1 ]]; then
        printf '%s\n' "${pods[0]}"
    elif [[ ${#pods[@]} -eq 0 ]]; then
        echo "ERROR: no pods in the fleet (checked with '$SENSIBO devices')" >&2
        exit 2
    else
        echo "ERROR: fleet has ${#pods[@]} pods; name one explicitly: ${pods[*]}" >&2
        exit 2
    fi
}

dispatch_control() {
    local verb="$1"; shift
    local extra=()
    case "$verb" in
        set)  extra+=(--target "$1"); shift ;;
        mode) extra+=(--mode "$1"); shift ;;
        fan)  extra+=(--fan "$1"); shift ;;
    esac

    local target=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --dry-run) DRY_RUN=1; shift ;;
            -h|--help) usage; exit 0 ;;
            *)         target="$1"; shift ;;
        esac
    done

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
    if [[ -n "$DRY_RUN" ]]; then
        # No --apply: the CLI prints exactly what it would do and changes nothing.
        :
    else
        cmd+=(--apply)
    fi

    echo "Running: ${cmd[*]}"
    exec "${cmd[@]}"
}

main() {
    local verb="${1:-}"
    [[ -n "$verb" ]] || { usage; exit 1; }
    shift

    case "$verb" in
        -h|--help)
            usage
            ;;
        status)
            "$SENSIBO" devices
            echo
            local pods=()
            mapfile -t pods < <(resolve_pods)
            if [[ ${#pods[@]} -eq 0 ]]; then
                echo "(no pods in the fleet)"
            else
                local p
                for p in "${pods[@]}"; do
                    "$SENSIBO" read "$p"
                    echo
                done
            fi
            ;;
        read)
            local loc="${1:-}"
            loc="$(resolve_target "$loc")"
            exec "$SENSIBO" read "$loc"
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
