# Architecture

How `sensibo-cli` is put together, and where the Sensibo code will go.

**Current state: scaffold.** Only the agent-first introspection verbs exist. This
document describes the skeleton that's here and the shape the rest must fit into.

## Names

Three names, all different — don't assume they match:

| | |
|---|---|
| PyPI dist | `sensibo-cli` |
| import package | `sensibo` |
| console command | `sensibo` |

## Zero runtime dependencies, deliberately

`pyproject.toml` declares `dependencies = []`. The runtime is stdlib-only;
`teken`, pytest, and the linters are dev-only.

This is a design constraint, not an accident, and it survives contact with the
real product: the Sensibo cloud API is plain REST/JSON with the key in a query
string, so a client is `urllib.request` + `json` + `gzip`. Adding a runtime
dependency is an architectural decision that needs justifying — see
[`sensibo-api.md`](sensibo-api.md) for why `pysensibo` in particular is a
reference and not a dependency.

The identity code makes the same trade: `whoami.read_agent_fields()` parses
`culture.yaml` with a hand-rolled line scanner rather than pulling in PyYAML.

## Layout

```text
sensibo/
  __init__.py          __version__ from package metadata
  __main__.py          python -m sensibo
  api/
    __init__.py        public surface: SensiboClient, ApiError family, scrub helpers
    _auth.py           key resolution: SENSIBO_API_KEY, then ~/.sensibo/.env
    _errors.py         ApiError - mirrors CliError's shape, does not import it
    _scrub.py          strip apiKey out of anything loggable
    client.py          SensiboClient - gzip, 429 backoff, pacing, endpoint wrappers
  cli/
    __init__.py        parser construction, dispatch, error trapping
    _errors.py         CliError + the exit-code policy
    _output.py         the stdout/stderr split
    _commands/         one module per verb, each exposing register(sub)
  explain/
    catalog.py         markdown keyed by command-path tuple
```

## The two load-bearing contracts

`teken cli doctor . --strict` enforces these, and CI runs it. Breaking either
fails the build.

### Error contract

Every failure raises `CliError(code, message, remediation)`. `main()` catches it
via `_dispatch()`, routes it through `_output.emit_error()`, and exits with
`code`. Two properties follow:

- **No Python traceback ever reaches stderr.** `_dispatch` wraps *any* unexpected
  exception into a `CliError` rather than letting it propagate.
- **Even argparse errors obey the contract.** `_CliArgumentParser.error()`
  overrides argparse's default `prog: error: …` / exit-2 behaviour. Because
  parse-time errors happen before `args.json` exists, `main()` pre-scans raw argv
  for `--json` and stashes the answer in the class-level `_json_hint`.

Exit codes: `0` success, `1` user error, `2` environment error, `3+` reserved.
In JSON mode an error is `{"code", "message", "remediation"}` on stderr; in text
mode it is an `error:` line plus a `hint:` line. **Agents parse the `hint:`
prefix — it is part of the contract, not decoration.**

### Stream contract

**Results to stdout. Diagnostics and errors to stderr. Never mixed.** Every
command takes `--json`. `_output.py` is the only module that writes to either
stream; go through it.

## Adding a verb

1. Create `sensibo/cli/_commands/<verb>.py` exposing `register(sub)`, which adds
   a subparser, adds `--json`, and calls `p.set_defaults(func=…)`.
2. Wire it into `_build_parser()` in `sensibo/cli/__init__.py`.
3. **Add a matching entry to `sensibo/explain/catalog.py`**, keyed by the
   command-path tuple. The rubric gate fails if a command has no `explain` entry.
4. Update `learn.py`'s command map — `learn` is the agent's front door and must
   stay accurate.

A noun with action-verbs must also expose `<noun> overview`; see
`_commands/cli.py` for the pattern.

### Write verbs: dry-run by default

**Every write verb previews by default and only acts on `--apply`.** This is
mandatory. Agents call this CLI in a loop, and a write here starts a compressor
in someone's home — a command that acts by accident is a bug, not a surprise.

Without `--apply`, a write verb prints exactly what it *would* do — the current
AC state, the target state, and the diff — and changes nothing.

## Where the Sensibo code goes

- **`sensibo/api/`** — **implemented.** A thin stdlib client
  (`SensiboClient` in `sensibo/api/client.py`). Owns the base URL, gzip
  (`Accept-Encoding: gzip`, transparent decode), key resolution and injection,
  429 backoff with jitter (bounded retries), client-side pacing between
  outbound calls, and **scrubbing the `apiKey` query parameter out of anything
  loggable** (`sensibo/api/_scrub.py` — the only place that knows the key is
  in the URL). Deliberately does not import from `sensibo/cli/`, so it is
  usable as a standalone library ahead of any CLI verb landing on top of it.
  Errors are `ApiError` (`sensibo/api/_errors.py`) — same `code` / `message`
  / `remediation` shape as `CliError`, but a separate type: the CLI layer maps
  one onto the other when the first verb built on this client lands.
  `historicalMeasurements` HTTP 403 is raised as a typed
  `GatedHistoryWindowError`, not a crash — see "History retention" in
  [`sensibo-api.md`](sensibo-api.md).
- **`sensibo/store/`** — the local time-series store. Not yet written. Must be
  schema-flexible: sensor sets differ per model, so persist "whatever fields
  this pod reported", and branch on `productModel` (the `pm25` unit trap in
  [`sensibo-api.md`](sensibo-api.md) is the reason).
- **`sensibo/rules/`** — the local rules engine. **Implemented.** A rule is
  declarative JSON (a name, a target pod, an `acState` action, and a tree of
  conditions over current store readings — thresholds, time-of-day windows,
  occupancy, sensor staleness (`{"type": "stale", "location": …,
  "after_seconds": …}`, which reads the health row the collector persists so a
  rule can refuse to trust a room that stopped reporting — see
  `examples/stale-room.rule.json`), and cross-room combinations addressing
  locations by name through
  `sensibo.store.resolve_location`). It imports the store and the API client and
  is imported by the CLI, but never imports `sensibo.cli` (same layering rule as
  the store). Three safety properties are load-bearing and tested: a per-pod
  minimum off-time / hysteresis (≥ 10 min, persisted across restarts) so a
  flapping condition cannot short-cycle a compressor; at most one write per pod
  per evaluation pass, through the API client's own rate limiting; and a rule
  cannot be *armed* until a `rule dry-run` of its current definition has run —
  editing the rule invalidates that fingerprint. Every rule and every
  rule-verb output declares `execution: local (stops when this daemon stops)`,
  the deliberate contrast with the cloud verbs (`smartmode`/`schedule`/`timer`).
  `sensibo rule run` is the only verb that drives the AC, and only for armed
  rules. A shipped example (`examples/cross-room-motion-temp.rule.json`) combines
  motion in one room with temperature in another — something Climate React
  cannot express.

The key resolves as `SENSIBO_API_KEY` in the environment first, then
`~/.sensibo/.env` (`sensibo/api/_auth.py`). It is never read from a committed
file and never echoed into output, logs, exceptions, or reprs.

## Identity and the backend

`culture.yaml` declares `backend: colleague` with a pinned Qwen model, so
**`AGENTS.colleague.md` is the mesh resident prompt** — the file the Culture mesh
daemon loads. `CLAUDE.md` is guidance for Claude Code sessions.

`doctor` enforces this via the `_PROMPT_FILE` map in `_commands/doctor.py`: the
declared backend must have its prompt file on disk (`claude` → `CLAUDE.md`,
`colleague` → `AGENTS.colleague.md`, `acp` → `AGENTS.md`, `gemini` →
`GEMINI.md`). Change `backend:` without adding the matching file and `doctor`
plus CI go red.

`whoami.find_culture_yaml()` walks up from `__file__`, not from the working
directory — the identity reported must be *this agent's own*, not whatever
`culture.yaml` happens to sit in the caller's CWD. In a wheel install no
`culture.yaml` ships, so it falls back to literal defaults and `doctor` reports a
single info check.
