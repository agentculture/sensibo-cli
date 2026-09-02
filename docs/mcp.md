# MCP server

`sensibo mcp serve` runs an [MCP](https://modelcontextprotocol.io) (Model
Context Protocol) server over stdio, giving MCP clients — Claude Code, Claude
Desktop, or any other MCP-speaking agent — the same read/control surface as
the CLI, without shelling out to it.

> **Unofficial community tool.** Sensibo is a trademark of Sensibo Ltd. This
> project is not affiliated with, endorsed by, or supported by them.

## Install

The core CLI stays zero-runtime-dependency (`pyproject.toml`'s `dependencies
= []`, deliberately — see [`architecture.md`](architecture.md)). The MCP
surface is the one part of this project allowed to need a real dependency
(the official [`mcp`](https://pypi.org/project/mcp/) Python SDK), so it ships
behind its own **optional extra**:

```bash
pip install "sensibo-cli[mcp]"
```

Without the extra, every other verb works exactly as before — `sensibo
--help`, `sensibo devices`, and so on never touch the `mcp` package at all.
Only `sensibo mcp serve` needs it, and it only imports the SDK once that verb
actually runs. If you run it without the extra installed, you get the usual
structured error, not a traceback:

```text
error: the 'mcp' package is not installed
hint: pip install "sensibo-cli[mcp]"
```

## Configure a client

The server speaks stdio: a client starts `sensibo mcp serve` as a subprocess
and talks MCP over its stdin/stdout. Every client needs three things: the
command (`sensibo`), the args (`mcp serve`), and `SENSIBO_API_KEY` in the
subprocess's environment (the same key resolution as the CLI —
`SENSIBO_API_KEY` first, then `~/.sensibo/.env`; see
[`sensibo-api.md`](sensibo-api.md)).

### Claude Code

Add it as a project- or user-scoped MCP server:

```bash
claude mcp add sensibo --env SENSIBO_API_KEY="$SENSIBO_API_KEY" -- sensibo mcp serve
```

Or add it directly to `.mcp.json` in the project root:

```json
{
  "mcpServers": {
    "sensibo": {
      "command": "sensibo",
      "args": ["mcp", "serve"],
      "env": {
        "SENSIBO_API_KEY": "your-api-key-here"
      }
    }
  }
}
```

### Claude Desktop

Add the same shape to `claude_desktop_config.json` (macOS:
`~/Library/Application Support/Claude/claude_desktop_config.json`; Windows:
`%APPDATA%\Claude\claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "sensibo": {
      "command": "sensibo",
      "args": ["mcp", "serve"],
      "env": {
        "SENSIBO_API_KEY": "your-api-key-here"
      }
    }
  }
}
```

Restart the client after editing either config file so it picks up the new
server.

## Tools

Five tools are wired onto the MCP server, each mirroring one CLI verb's exact
behaviour so the MCP surface and the CLI surface never give two different
answers to the same request:

| Tool | Mirrors | What it does |
|------|---------|--------------|
| `list_devices` | `sensibo devices` | The fleet from one API call: every pod's id, product model, room, connection status, and the sensor field *names* it actually reports — plus each pod's nested Room Sensor locations. Read-only. |
| `read_location` | `sensibo read` | Current readings for one location, by stable id, operator alias, or Sensibo room name (alias/name resolved against the local store first). Read-only. |
| `query_history` | `sensibo query` | Offline reads from the **local store only** — never touches the network. `mode="latest"` (default) or `mode="range"`, by location and field. |
| `set_ac_state` | `sensibo set` | Control an AC's power/mode/target/fan/swing. |
| `room_list` | `sensibo room list` | Every known sensing location: id, kind, model, room name, alias, last-seen, and a staleness flag. Local store only. |

As of task t9, `room_list`'s staleness threshold defaults to
`sensibo.health.model.HealthConfig.from_env().down_after_seconds` (the same
source of truth the web dashboard and `sensibo room list` use — see
[`web.md`](web.md), "Staleness: one source of truth"), not a hardcoded 24h,
and each row also carries `health_status`/`health_since`/`health_last_ok`
from the health table when a row exists for that location (`None` when it
doesn't — callers fall back to `stale`).

### `sensibo_health` (task t9)

`sensibo_health(since=None, db=None)` is registered on the MCP server
alongside the five tools above (`sensibo.mcp_server.build_server`). It
mirrors `sensibo query health --json`: every location's current health row
(`status` — one of `ok` / `down` / `unknown` / `unknown_parent_down` —
`since`, `last_ok`, `parent_pod_id`) plus every transition since an optional
ISO 8601 `since`
timestamp, each transition carrying `duration_seconds` when it is the one
that *closed* an outage (a transition back to `ok`) and `None` otherwise
(including an outage still open). The payload also reports the collector's
own heartbeat, `last_cycle_at` / `last_cycle_outcome` — two store-level facts
`sensibo collect` writes every poll cycle — so a client can tell "no alerts"
apart from "the collector stopped running". Local store only, read-only.

### The `apply`-defaults-to-`false` contract

`set_ac_state` is the one write tool, and it carries the exact same safety
contract as the CLI's own `sensibo set` (`docs/architecture.md`, "Write
verbs: dry-run by default"): **`apply` defaults to `false`.**

- **`apply=false`** (the default — an MCP client can omit the parameter
  entirely): reads the pod's current `acState` and returns the diff of what
  *would* change. Zero write requests are issued.
- **`apply=true`**: commits the change — a single changed field goes through
  the safe single-property `PATCH`; two or more changed fields go through the
  full-state `POST`. Either way, the resulting state is read back and
  returned, never assumed.

This means an MCP client (an LLM agent, typically) can call `set_ac_state`
freely to preview a change — turning on a compressor is never a side effect
of an exploratory tool call — and only commits when it explicitly passes
`apply: true`.

```json
// dry run: pod-abc123's mode changes from "heat" to "cool"; nothing written
{"pod_id": "pod-abc123", "mode": "cool"}

// commits it
{"pod_id": "pod-abc123", "mode": "cool", "apply": true}
```

## See also

- [`architecture.md`](architecture.md) — the dry-run-by-default contract
  every write verb (CLI or MCP) honours.
- [`api.md`](api.md) — the plain Python import surface, for callers that
  don't want MCP or the CLI at all.
- [`sensibo-api.md`](sensibo-api.md) — the underlying Sensibo API reference,
  the per-model sensor traps, and key resolution.
