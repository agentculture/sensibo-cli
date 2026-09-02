# Skill upstream sources

sensibo-cli vendors its `.claude/skills/` from **guildmaster** — the
AgentCulture **skills supplier** after the steward → guildmaster cutover
(guildmaster 0.5.0, 2026-05-24). `steward` retains the **alignment** role
(`steward doctor`, the sibling-pattern baseline); only the skills-supplier role
moved. This file tracks provenance so re-syncs stay deterministic.

Eight skills originate in
[`agentculture/devague`](https://github.com/agentculture/devague). Three of them
(`think`, `spec-to-plan`, `assign-to-workforce`) are **re-broadcast** through
guildmaster — cite guildmaster's copy; track devague as the true origin. The
other five (`scope`, `challenge`, `deviate`, `validate-delivery`,
`summarize-delivery`) are method-only (a `SKILL.md`, no `scripts/` resolver)
and are vendored **directly from the sibling `devague` checkout** — see the
[devague method-only skills](#devague-method-only-skills-vendored-directly)
section below. One skill, `ask-colleague` (formerly `outsource`), originates in
[`agentculture/colleague`](https://github.com/agentculture/colleague) — the
renamed `convertible`. guildmaster's re-broadcast still carries the old
`outsource` name, so `ask-colleague` is vendored **directly from colleague** as a
tracked local divergence (see [below](#local-divergence--outsource--ask-colleague-2026-06-06)).

Every vendored `SKILL.md` carries `type: command`. sensibo-cli
declares a culture agent (`culture.yaml`, `backend: colleague`), and
`core.skill_loader` silently skips any `SKILL.md` lacking `type:` — so the field
is load-bearing, even where guildmaster's upstream copy omits it.

| Skill | Upstream | Origin | Notes | Last synced |
|-------|----------|--------|-------|-------------|
| `cicd` | `../guildmaster/.claude/skills/cicd/` | guildmaster | CI/CD lane layered on `devex pr`: the 5 thin scripts (`workflow.sh`, `pr-status.sh`, `pr-reply.sh`, `_resolve-nick.sh`, `portability-lint.sh`) delegate lint/open/read/reply/delta to `devex` and add the `status` / `await` SonarCloud-gating extensions. Consumer-identifying prose (`guildmaster` → `sensibo-cli`) adapted in the description + heading; upstream history (`Renamed from pr-review in steward 0.7.0; rebased on devex in 0.12.0`) and env-var literals (`STEWARD_*`) kept verbatim. The PR signature resolves at runtime from `culture.yaml` via `_resolve-nick.sh` (→ `sensibo-cli`). Requires `devex` on PATH. | 2026-05-26 (guildmaster 0.6.0) |
| `communicate` | `../guildmaster/.claude/skills/communicate/` | guildmaster | Cross-repo + mesh communication. Consumer-identifying prose adapted in the description (incl. the `- sensibo-cli (Claude)` signature line). **No hard-coded signature literal in the scripts** — `post-issue.sh` is `agtag`-backed and resolves the signing nick from `culture.yaml`; requires `agtag` (>=0.1) on PATH. The supplier `scripts/templates/` (`skill-update-brief.md`, `skill-new-brief.md`) are kept verbatim — inert for a consumer (they cite guildmaster as upstream). Renamed from `coordinate` in steward 0.8.0; absorbed `gh-issues` in 0.9.1. | 2026-05-26 (guildmaster 0.6.0) |
| `version-bump` | `../guildmaster/.claude/skills/version-bump/` | guildmaster | Pure-Python, CWD-aware (`scripts/bump.py`). Verbatim except added `type: command`. | 2026-05-26 (guildmaster 0.6.0) |
| `agent-config` | `../guildmaster/.claude/skills/agent-config/` | guildmaster (origin steward) | Shows a Culture agent's full config; run `scripts/show.sh` directly (no `guild` binary required). `scripts/show.sh` + `data/backend-fingerprints.yaml` verbatim. Verbatim except added `type: command`. | 2026-05-26 (guildmaster 0.6.0) |
| `doc-test-alignment` | `../guildmaster/.claude/skills/doc-test-alignment/` | guildmaster | **STUB** — `scripts/check.sh` exits not-yet-implemented; the contract lives in SKILL.md. Verbatim except added `type: command`. | 2026-05-26 (guildmaster 0.6.0) |
| `pypi-maintainer` | `../guildmaster/.claude/skills/pypi-maintainer/` | guildmaster | Switch a package install between PyPI / TestPyPI / local editable (`scripts/switch-source.sh`). Verbatim except added `type: command`. | 2026-05-26 (guildmaster 0.6.0) |
| `run-tests` | `../guildmaster/.claude/skills/run-tests/` | guildmaster | pytest + xdist + coverage (`scripts/test.sh`). Verbatim except added `type: command`. | 2026-05-26 (guildmaster 0.6.0) |
| `sonarclaude` | `../guildmaster/.claude/skills/sonarclaude/` | guildmaster | SonarCloud API queries (`scripts/sonar.sh`). Verbatim except added `type: command`. | 2026-05-26 (guildmaster 0.6.0) |
| `think` | `../guildmaster/.claude/skills/think/` | **devague** (re-broadcast via guildmaster) | idea→spec leg of the devague workflow chain. Verbatim (already carried `type: command` at guildmaster). Origin/broadcast prose left verbatim. | 2026-05-26 (guildmaster 0.6.0) |
| `spec-to-plan` | `../guildmaster/.claude/skills/spec-to-plan/` | **devague** (re-broadcast via guildmaster) | spec→plan leg of the devague workflow chain. Verbatim (already carried `type: command`). | 2026-05-26 (guildmaster 0.6.0) |
| `assign-to-workforce` | `../guildmaster/.claude/skills/assign-to-workforce/` | **devague** (re-broadcast via guildmaster) | plan→parallel-implementation leg of the devague workflow chain. Verbatim (already carried `type: command`). | 2026-05-26 (guildmaster 0.6.0) |
| `scope` | `../devague/.claude/skills/scope/` | **devague** (direct) | idea→scope leg: read-only surface survey before `/think`; findings land via the deterministic `devague scope` move with `--seeds` provenance. Method-only. Verbatim (carries `type: command`). | 2026-09-02 (devague 0.23.0) |
| `challenge` | `../devague/.claude/skills/challenge/` | **devague** (direct) | blind-spot pass between `/think` and `/spec-to-plan`. Method-only. Verbatim. | 2026-09-02 (devague 0.23.0) |
| `deviate` | `../devague/.claude/skills/deviate/` | **devague** (direct) | records an execution-time deviation from a confirmed plan via `devague deviate`. Method-only. Verbatim. | 2026-09-02 (devague 0.23.0, unchanged) |
| `validate-delivery` | `../devague/.claude/skills/validate-delivery/` | **devague** (direct) | execution-to-evidence leg: runs the plan's behavioral tests agent-side after `assign-to-workforce`, files `evidence` / `delta` records via the CLI, never inside it. Method-only. Verbatim. | 2026-09-02 (devague 0.23.0) |
| `summarize-delivery` | `../devague/.claude/skills/summarize-delivery/` | **devague** (direct) | closes the loop: planned vs actual, evidence-backed delivery claims, drift. Method-only. Verbatim. | 2026-09-02 (devague 0.23.0) |
| `ask-colleague` | `../colleague/.claude/skills/ask-colleague/` | **colleague** (renamed from convertible; vendored directly — guildmaster re-broadcast pending) | The first-party front door to the `colleague` CLI: hand a scoped task to a *different* engine/mind via `explore` / `review` / `write`, grade a finished work item via `feedback` (the ROI loop), and reap stale/corrupt `colleague/*` branches a crashed run left behind via `clean`. Every verb takes `--json` (result JSON on stdout, diagnostics on stderr). `explore`/`review` run isolated in a throwaway `git worktree`; `write` **previews by default** (throwaway worktree, no side effects) and refuses a dirty tree only when applying (`--apply` / `--pr`). Verbatim except one consumer-identifying clause in the Provenance paragraph (`colleague vendors from guildmaster` → `sensibo-cli vendors from guildmaster`); already carried `type: command`. Optional runtime dep: **`colleague`** on PATH. | 2026-06-12 (colleague 1.7.0, direct) |

## devague method-only skills (vendored directly)

`scope`, `challenge`, `deviate`, `validate-delivery`, and `summarize-delivery`
have no `scripts/` directory — each `SKILL.md` invokes the `devague` CLI
directly (`uv tool install devague`). They are pulled straight from the
`devague` origin rather than guildmaster's re-broadcast, so a re-sync is:

```bash
for s in scope challenge deviate validate-delivery summarize-delivery; do
  diff -u ../devague/.claude/skills/$s/SKILL.md .claude/skills/$s/SKILL.md
  cp ../devague/.claude/skills/$s/SKILL.md .claude/skills/$s/SKILL.md
done
```

No adaptations are applied — devague's copies already carry `type: command`
and contain no consumer-identifying prose. `devague learn skills` teaches the
same eight-skill family and its authoring recipe.

## Local skills

Skills authored in this repo — no upstream, outside the re-sync procedure
above:

| Skill | Upstream | Origin | Notes | Added |
|-------|----------|--------|-------|-------|
| `manage-ac` | — (local) | sensibo-cli | Operating quick-reference for the fleet via the installed `sensibo` CLI: reads (`devices` / `read` / `query` / `room list`), writes (`set`, dry-run + `--apply`), naming (`room name`), automation (local `rule`; cloud `schedule` / `timer` / `smartmode`). SKILL.md plus `scripts/ac.sh`, a one-shot control wrapper (`status` / `read` / `on` / `off` / `set` / `mode` / `fan`, `[pod\|all]`, `--json`, `--dry-run`; control verbs commit via `sensibo set --apply` by design, `status` costs one fleet API call). The CLI stays the interface and `sensibo learn` / `sensibo explain <noun> [verb]` the authoritative docs. Carries `type: command` per the skill_loader contract. | 2026-08-23 |

## Qwen Code bridge

`.qwen/skills` is a relative symlink to `.claude/skills`, so the same skill
tree is also discoverable as Qwen Code project skills — no second copy, no
drift. Qwen Code's loader follows symlinks (top-level directory or per-skill
entries) and its frontmatter validation requires only `name` + `description`,
so the vendored `type: command` field (load-bearing for Claude and the
culture `core.skill_loader`) is accepted unmodified.

## Re-sync procedure

```bash
# Diff against upstream before pulling (example: cicd / communicate):
for s in cicd communicate; do
  diff -ru ../guildmaster/.claude/skills/$s .claude/skills/$s
done

# Pull a skill fresh (remove first so dropped scripts don't linger):
rm -rf .claude/skills/<skill>
cp -R ../guildmaster/.claude/skills/<skill> .claude/skills/

# Re-apply the identifier-only adaptations in SKILL.md:
#   - consumer-identifying prose: `guildmaster` → `sensibo-cli` (NOT
#     where it cites guildmaster/steward/devague as the upstream/origin).
#   - add `type: command` to the frontmatter if guildmaster's copy omits it
#     (load-bearing for the culture/claude backend's core.skill_loader).
# No script bodies are edited (cite-don't-import). The communicate signature
# resolves from culture.yaml via agtag — no literal to patch.
```

If a re-sync would lose a sensibo-cli adaptation, lift the change
upstream into guildmaster first (per guildmaster's `docs/skill-sources.md`) and
re-vendor.

### Local divergence — `agex` → `devex` rename (2026-05-30)

The PR-lifecycle CLI was renamed `agex` → `devex` (same tool, new name). The
vendored `cicd` (`SKILL.md`, `workflow.sh`, `pr-status.sh`),
`assign-to-workforce`, and `communicate` (`skill-new-brief.md` template) copies
were **patched in place** for this rename rather than re-vendored — a deliberate
exception to cite-don't-import, made so the `cicd` scripts invoke the real
`devex pr` binary now. The matching canonical rename is tracked upstream for
guildmaster in [agentculture/guildmaster#48](https://github.com/agentculture/guildmaster/issues/48),
so the next clean re-sync from guildmaster reconciles without losing this
change. (Re-sync once guildmaster's renamed copies are broadcast.)

The same in-place patch also bumped the documented `devex` version floor from
`>=0.1` to `>=0.21` in the vendored `cicd` `SKILL.md` + `workflow.sh` (to match
this doc's tooling-prerequisites and the `await`-era feature set) — likewise
flagged for guildmaster on #48.

### Local divergence — outsource → ask-colleague (2026-06-06)

`convertible` was renamed **`colleague`**, and its skill `outsource` →
**`ask-colleague`** (colleague#148; the `wheels` verb also became `backends`, and
`drive` → `work`). `ask-colleague` adds a fourth verb, `feedback` (the ROI loop),
and `write` now **previews by default** (a throwaway worktree, no side effects)
instead of committing to a branch unless you pass `--apply` / `--pr`.

guildmaster has **not** re-broadcast the rename yet — its kit still ships the old
`outsource`. So this template's `outsource/` was removed and `ask-colleague/`
vendored **directly from the sibling `colleague` checkout**
(`../colleague/.claude/skills/ask-colleague/`), not from guildmaster. This is a
tracked exception to "cite guildmaster's copy", parallel to the `agex` → `devex`
divergence above. Re-sync path until guildmaster catches up:

```bash
# Pull ask-colleague fresh from colleague (the origin):
rm -rf .claude/skills/ask-colleague
cp -R ../colleague/.claude/skills/ask-colleague .claude/skills/
# Re-apply the one consumer-identifying clause in SKILL.md Provenance:
#   `which colleague vendors from guildmaster`
#     → `which sensibo-cli vendors from guildmaster`
# (already carries `type: command`; no script bodies edited.)
```

Once guildmaster re-broadcasts `ask-colleague`, switch the upstream column back
to `../guildmaster/.claude/skills/ask-colleague/` and re-sync from there.

## Tooling prerequisites

- **`devex`** (>=0.21) on PATH — `cicd` delegates the PR lifecycle to `devex pr`.
- **`agtag`** (>=0.1) on PATH — `communicate` issue I/O wraps `agtag issue`.

Both ship on PATH in the standard AgentCulture dev setup (installed per the
devex / agtag READMEs).

- **`colleague`** on PATH — *optional*; only the `ask-colleague` skill needs it,
  and only when invoked (`uv tool install colleague`). The wrapper exits
  with a clear install hint if it is absent, so the skill degrades gracefully
  rather than blocking a clone that never uses it. `ask-colleague` also needs a
  reachable backend — a local vLLM by default, overridable via `--engine` /
  `--model` / `--base-url` or `COLLEAGUE_*` env (the legacy `CONVERTIBLE_*` names
  still work as a deprecated fallback).
