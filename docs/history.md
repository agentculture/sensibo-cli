# Project history / before-state

## Frame time: 2026-07-14

The full-product build (control, collection, automation, and the
Python/MCP/web integration surfaces) was framed against a specific,
checkable before-state, recorded here so the "before" side of that frame
never has to be taken on trust.

At the spec commit — `f373915` ("spec: full product frame — control, 2y
collection, automations, import/MCP/web (devague /think)"), dated
2026-07-14 — the repo was **a scaffold**. It shipped only the agent-first
introspection verbs inherited from `culture-agent-template`:

- `whoami` — identity from `culture.yaml`
- `learn` — structured self-teaching prompt
- `explain <path>` — markdown docs for any noun/verb path
- `overview` — read-only descriptive snapshot of the agent
- `doctor` — agent-identity invariants
- `cli overview` — describe the CLI surface itself

There was **no AC control code, no collection code, and no rules code**.
None of the three product pillars (control the AC, collect sensor history
into a local store, automate conditions that drive the AC) existed yet —
only the introspection scaffold did.

## Why this is recorded

The spec's honesty conditions require the before-state to be checkable, not
just asserted:

> The before-state is as described: at frame time the repo ships only
> introspection verbs, with no control, collect, or rules code.

Anchoring the claim to the frame commit hash means anyone can verify it
directly:

```bash
git show f373915 --stat
```

See
[`docs/specs/2026-07-14-sensibo-cli-ships-the-full-product-one-cli-and-age.md`](specs/2026-07-14-sensibo-cli-ships-the-full-product-one-cli-and-age.md)
for the full frame and
[`docs/plans/2026-07-14-sensibo-cli-ships-the-full-product-one-cli-and-age.md`](plans/2026-07-14-sensibo-cli-ships-the-full-product-one-cli-and-age.md)
for how it was broken into build tasks.
