"""HTML page rendering for the web dashboard (task t12).

Plain string templates, ``html.escape`` at every insertion point, no external
assets and no JavaScript framework — matching the product's stdlib-only
constraint (``docs/architecture.md``, "Zero runtime dependencies"). Pages
render entirely from data already fetched from the local store (or, for the
control pages, from a dry-run/apply result computed by
:mod:`sensibo.web.server`) — this module does no I/O of its own.
"""

from __future__ import annotations

import html as _html

from sensibo.health.model import STATUS_OK
from sensibo.store import HealthRecord, LocationRecord, ReadingRecord
from sensibo.store.rooms import is_stale

from ._svg import render_sparkline
from ._wire import display_name, format_iso

_DISCLAIMER = (
    "Unofficial community tool. Sensibo is a trademark of Sensibo Ltd; this "
    "project is not affiliated with, endorsed by, or supported by them."
)

_STYLE = """\
<style>
  :root { color-scheme: light dark; }
  body { font-family: system-ui, -apple-system, sans-serif; margin: 2rem auto; max-width: 960px; \
line-height: 1.4; }
  h1, h2, h3 { line-height: 1.2; }
  table { border-collapse: collapse; width: 100%; margin: 0.75rem 0; }
  th, td { text-align: left; padding: 0.35rem 0.6rem; border-bottom: 1px solid #88888844; }
  .stale { color: #b45309; font-weight: 600; }
  .status-badge { font-weight: 600; }
  .status-down, .status-unknown, .status-unknown_parent_down { color: #b45309; }
  .status-ok { color: #15803d; }
  .card { border: 1px solid #88888844; border-radius: 10px; padding: 1rem 1.25rem; margin: 1rem 0; }
  .muted { opacity: 0.75; font-size: 0.9em; }
  .control label { display: inline-block; min-width: 5.5rem; }
  .field { margin: 0.35rem 0; }
  .banner-error { background: #fee2e2; color: #7f1d1d; padding: 0.6rem 1rem; border-radius: 8px; }
  .banner-ok { background: #dcfce7; color: #14532d; padding: 0.6rem 1rem; border-radius: 8px; }
  a { color: inherit; }
  code { font-size: 0.95em; }
</style>
"""


def _page(title: str, body: str) -> str:
    return (
        '<!doctype html>\n<html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_html.escape(title)}</title>{_STYLE}</head><body>{body}</body></html>\n"
    )


def _footer() -> str:
    return f'<p class="muted">{_html.escape(_DISCLAIMER)}</p>'


# -- health badge (task t9) -------------------------------------------------


def _health_badge(
    loc: LocationRecord, *, stale_after_hours: float, health: HealthRecord | None
) -> str:
    """A location's staleness marker: the health table's own status/since/last_ok
    when a row exists, falling back to the derived STALE flag when it does not.
    """
    if health is not None:
        css = "" if health.status == STATUS_OK else " stale"
        last_ok_text = format_iso(health.last_ok) if health.last_ok is not None else "never"
        return (
            f' <span class="status-badge status-{_html.escape(health.status)}{css}">'
            f"{_html.escape(health.status)}</span>"
            f'<span class="muted"> since {_html.escape(format_iso(health.since))}; '
            f"last ok {_html.escape(last_ok_text)}</span>"
        )
    stale = is_stale(loc.last_seen, stale_after_hours=stale_after_hours)
    return ' <span class="stale">STALE</span>' if stale else ""


def _heartbeat(last_cycle_at: str | None, last_cycle_outcome: str | None) -> str:
    at_text = _html.escape(last_cycle_at) if last_cycle_at else "never"
    outcome_text = _html.escape(last_cycle_outcome) if last_cycle_outcome else "unknown"
    return (
        f'<p class="muted">collector heartbeat: last cycle at {at_text} '
        f"(outcome: {outcome_text})</p>"
    )


def _reports_section(reports: list[str] | None) -> str:
    reports = reports or []
    if not reports:
        return "<h2>Reports</h2><p>(no reports yet)</p>"
    items = "".join(
        f'<li><a href="/reports/{_html.escape(name)}">{_html.escape(name)}</a></li>'
        for name in reports
    )
    return f"<h2>Reports</h2><ul>{items}</ul>"


# -- index ---------------------------------------------------------------


def render_index(
    rows: list[tuple[LocationRecord, dict[str, ReadingRecord], HealthRecord | None]],
    *,
    stale_after_hours: float,
    last_cycle_at: str | None = None,
    last_cycle_outcome: str | None = None,
    reports: list[str] | None = None,
) -> str:
    items: list[str] = []
    if not rows:
        items.append("<p>No locations yet — run <code>sensibo collect</code> first.</p>")
    for loc, latest, health in sorted(rows, key=lambda row: display_name(row[0]).lower()):
        flag = _health_badge(loc, stale_after_hours=stale_after_hours, health=health)
        name = _html.escape(display_name(loc))
        summary = (
            ", ".join(
                f"{field}={reading.value}{(' ' + reading.unit) if reading.unit else ''}"
                for field, reading in sorted(latest.items())
            )
            or "(no readings yet)"
        )
        items.append(
            f'<div class="card"><h2><a href="/location/{_html.escape(loc.id)}">{name}</a>'
            f"{flag}</h2>"
            f"<p>kind: {_html.escape(loc.kind)} &middot; "
            f"model: {_html.escape(loc.product_model or '-')} &middot; "
            f"last seen: {_html.escape(format_iso(loc.last_seen))}</p>"
            f"<p>{_html.escape(summary)}</p></div>"
        )
    body = (
        "<h1>sensibo-cli &mdash; LAN dashboard</h1>"
        "<p>Reads are open on this network; writes require the token in "
        "<code>~/.sensibo/web-token</code> (or the path given to "
        "<code>--token-file</code>).</p>"
        + _heartbeat(last_cycle_at, last_cycle_outcome)
        + "".join(items)
        + _reports_section(reports)
        + _footer()
    )
    return _page("sensibo-cli dashboard", body)


# -- location detail -------------------------------------------------------


def render_location(
    loc: LocationRecord,
    latest: dict[str, ReadingRecord],
    history: dict[str, list[ReadingRecord]],
    *,
    stale_after_hours: float,
    health: HealthRecord | None = None,
    banner: str | None = None,
) -> str:
    name = _html.escape(display_name(loc))
    flag = _health_badge(loc, stale_after_hours=stale_after_hours, health=health)
    parts = ['<p><a href="/">&larr; all locations</a></p>', f"<h1>{name}{flag}</h1>"]
    if banner:
        parts.append(banner)
    parts.append(
        f"<p>id: <code>{_html.escape(loc.id)}</code> &middot; "
        f"kind: {_html.escape(loc.kind)} &middot; "
        f"model: {_html.escape(loc.product_model or '-')} &middot; "
        f"last seen: {_html.escape(format_iso(loc.last_seen))}</p>"
    )

    parts.append("<h2>Latest readings</h2>")
    if not latest:
        parts.append("<p>(no readings yet)</p>")
    else:
        parts.append("<table><tr><th>field</th><th>value</th><th>unit</th></tr>")
        for field, reading in sorted(latest.items()):
            parts.append(
                f"<tr><td>{_html.escape(field)}</td>"
                f"<td>{_html.escape(str(reading.value))}</td>"
                f"<td>{_html.escape(reading.unit or '')}</td></tr>"
            )
        parts.append("</table>")

    parts.append("<h2>History</h2>")
    if not history:
        parts.append("<p>(no history yet)</p>")
    for field in sorted(history):
        parts.append(f"<h3>{_html.escape(field)}</h3>")
        parts.append(render_sparkline(history[field]))

    if loc.kind == "pod":
        parts.append(render_control_form(loc.id))

    parts.append(_footer())
    return _page(f"{display_name(loc)} — sensibo-cli", "".join(parts))


# -- control form / result --------------------------------------------------


#: The control form's static markup. Deliberately a **plain** (non f-string,
#: non ``%``/``.format``) literal: bandit's B608 SQL-injection heuristic
#: false-positives on any *dynamically formatted* string containing the word
#: "select" (matching this form's `<select>` HTML tags), so the one dynamic
#: piece — the hidden `pod_id` input — is built as its own tiny f-string and
#: substituted in via `.format()`-free `+` concatenation below instead of
#: being interpolated into this block directly.
_CONTROL_FORM_TEMPLATE = """\
<div class="card">
  <h2>Control</h2>
  <p class="muted">Submitting previews the change (zero writes). A second,
explicit confirm applies it.</p>
  <form class="control" method="post" action="/control">
    {hidden_pod_input}
    <div class="field"><label for="power">Power</label>
      <select name="power" id="power">
        <option value="">(unchanged)</option>
        <option value="on">on</option>
        <option value="off">off</option>
      </select>
    </div>
    <div class="field"><label for="mode">Mode</label>
      <select name="mode" id="mode">
        <option value="">(unchanged)</option>
        <option value="cool">cool</option>
        <option value="heat">heat</option>
        <option value="fan">fan</option>
        <option value="dry">dry</option>
        <option value="auto">auto</option>
      </select>
    </div>
    <div class="field"><label for="target">Target</label>
      <input type="number" name="target" id="target" placeholder="(unchanged)"></div>
    <div class="field"><label for="fan">Fan</label>
      <input type="text" name="fan" id="fan" placeholder="(unchanged)"></div>
    <div class="field"><label for="swing">Swing</label>
      <input type="text" name="swing" id="swing" placeholder="(unchanged)"></div>
    <div class="field"><label for="token">Token</label>
      <input type="password" name="token" id="token" required
             placeholder="from ~/.sensibo/web-token"></div>
    <button type="submit">Preview change (dry run)</button>
  </form>
</div>
"""


def render_control_form(pod_id: str) -> str:
    hidden_pod_input = f'<input type="hidden" name="pod_id" value="{_html.escape(pod_id)}">'
    return _CONTROL_FORM_TEMPLATE.replace("{hidden_pod_input}", hidden_pod_input)


def _diff_table(changes: dict[str, dict[str, object]]) -> str:
    rows = "".join(
        f"<tr><td>{_html.escape(field)}</td>"
        f"<td>{_html.escape(str(change['from']))}</td>"
        f"<td>{_html.escape(str(change['to']))}</td></tr>"
        for field, change in changes.items()
    )
    return f"<table><tr><th>field</th><th>from</th><th>to</th></tr>{rows}</table>"


def render_control_result(payload: dict[str, object], *, form: dict[str, str]) -> str:
    """Render a dry-run or applied control result, from :func:`_process_pod`'s payload.

    ``form`` is the original submitted fields (minus ``confirm``/``token``),
    replayed as hidden inputs on the confirm form so the second POST repeats
    the identical requested change.
    """
    pod_id = str(payload["pod_id"])
    changes = payload["changes"]
    applied = bool(payload.get("applied"))
    escaped_pod = _html.escape(pod_id)
    parts = [f'<p><a href="/location/{escaped_pod}">&larr; back to {escaped_pod}</a></p>']

    if not changes:
        parts.append(
            '<div class="banner-ok">Already matches the requested state; nothing to change.</div>'
        )
        return _page("sensibo-cli — control result", "".join(parts) + _footer())

    parts.append("<h2>Requested change</h2>")
    parts.append(_diff_table(changes))

    if applied:
        parts.append('<div class="banner-ok">Applied.</div>')
        result_state = payload.get("result_ac_state")
        if isinstance(result_state, dict):
            parts.append("<h3>State after apply</h3><pre>")
            for key, value in sorted(result_state.items()):
                parts.append(f"{_html.escape(key)}: {_html.escape(str(value))}\n")
            parts.append("</pre>")
    else:
        parts.append(
            '<div class="banner-ok">DRY RUN &mdash; nothing changed yet. '
            "Submit again to confirm and apply.</div>"
        )
        hidden = "".join(
            f'<input type="hidden" name="{_html.escape(name)}" value="{_html.escape(value)}">'
            for name, value in form.items()
            if name != "confirm" and value
        )
        parts.append(
            f'<form method="post" action="/control">{hidden}'
            '<input type="hidden" name="confirm" value="1">'
            '<button type="submit">Confirm and apply</button></form>'
        )

    parts.append(_footer())
    return _page("sensibo-cli — control result", "".join(parts))


def render_error(message: str, *, status: int = 400) -> str:
    return _page(
        "sensibo-cli — error",
        f'<div class="banner-error">error ({status}): {_html.escape(message)}</div>' + _footer(),
    )
