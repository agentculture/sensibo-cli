"""CLI tests for ``sensibo rule`` — the local rules engine (task t9).

Every test drives :func:`sensibo.cli.main` with ``--rules``/``--db`` pointed at
``tmp_path`` (never the real ``~/.sensibo``), and any ``rule run`` mocks the AC
client seam (``sensibo.cli._commands.rule.build_client``) so no test makes, or
could make, a real network call or drive a real air conditioner.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import pytest

import sensibo.cli._commands.rule as rule_module
from sensibo.cli import main
from sensibo.store import Store

_EXECUTION_STR = "local (stops when this daemon stops)"


# --- mock AC client ----------------------------------------------------------


class _FakeClient:
    def __init__(self, pods: dict[str, dict[str, Any]]) -> None:
        self._pods = {pid: dict(state) for pid, state in pods.items()}
        self.calls: list[tuple] = []

    def get_pod(self, pod_id: str, fields: str | None = None) -> dict[str, Any]:
        self.calls.append(("get_pod", pod_id))
        return {"result": {"acState": dict(self._pods[pod_id])}}

    def patch_ac_state(
        self, pod_id: str, prop: str, current_ac_state: dict[str, Any], new_value: object
    ) -> dict[str, Any]:
        self.calls.append(("patch_ac_state", pod_id, prop, new_value))
        self._pods[pod_id][prop] = new_value
        return {"result": dict(self._pods[pod_id])}

    def post_ac_states(self, pod_id: str, ac_state: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("post_ac_states", pod_id, dict(ac_state)))
        self._pods[pod_id].update(ac_state)
        return {"result": dict(self._pods[pod_id])}


def _install_client(monkeypatch: pytest.MonkeyPatch, fake: _FakeClient) -> None:
    monkeypatch.setattr(rule_module, "build_client", lambda: fake)


@pytest.fixture()
def block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Blow up on any socket use — proves read-only verbs never hit the network."""

    def _blocked(*_a: object, **_k: object) -> None:
        raise OSError("network disabled for this test")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)


# --- fixtures ----------------------------------------------------------------


def _seed_store(db: Path, *, temp: float = 27.0, pod: str = "ac1") -> None:
    with Store(db_path=db) as store:
        store.upsert_location(pod, kind="pod", product_model="airq")
        store.record_reading(pod, "temperature", temp, timestamp=1000.0)


def _add_inline_rule(rules: Path, name: str = "cool-when-hot", pod: str = "ac1") -> int:
    return main(
        [
            "rule",
            "add",
            "--name",
            name,
            "--pod",
            pod,
            "--power",
            "on",
            "--mode",
            "cool",
            "--when-location",
            pod,
            "--when-field",
            "temperature",
            "--when-op",
            ">",
            "--when-value",
            "26",
            "--rules",
            str(rules),
        ]
    )


# --- add ---------------------------------------------------------------------


def test_add_inline_creates_a_disarmed_rule(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rules = tmp_path / "rules.json"
    rc = _add_inline_rule(rules)
    assert rc == 0
    out = capsys.readouterr().out
    assert _EXECUTION_STR in out

    rc = main(["rule", "list", "--rules", str(rules), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["execution"] == _EXECUTION_STR
    assert len(payload["rules"]) == 1
    row = payload["rules"][0]
    assert row["name"] == "cool-when-hot"
    assert row["armed"] is False
    assert row["dry_run_current"] is False


def test_add_from_file_supports_the_cross_room_example(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rules = tmp_path / "rules.json"
    example = Path(__file__).resolve().parents[1] / "examples" / "cross-room-motion-temp.rule.json"
    rc = main(["rule", "add", "--file", str(example), "--rules", str(rules), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["rule"]["name"] == "cool-bedroom-when-hallway-busy"
    assert payload["rule"]["execution"] == _EXECUTION_STR
    assert payload["armed"] is False


def test_add_without_a_condition_is_a_user_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rules = tmp_path / "rules.json"
    rc = main(
        ["rule", "add", "--name", "x", "--pod", "ac1", "--power", "on", "--rules", str(rules)]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err


# --- dry-run is read-only and unlocks arming ---------------------------------


def test_dry_run_is_read_only_and_reports_would_fire(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], block_network: None
) -> None:
    rules = tmp_path / "rules.json"
    db = tmp_path / "sensibo.db"
    _seed_store(db, temp=27.0)
    _add_inline_rule(rules)
    capsys.readouterr()

    rc = main(
        ["rule", "dry-run", "cool-when-hot", "--rules", str(rules), "--db", str(db), "--json"]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["would_fire"] is True
    assert payload["execution"] == _EXECUTION_STR

    # dry-run recorded the fingerprint, so the rule is now armable.
    rc = main(["rule", "arm", "cool-when-hot", "--rules", str(rules)])
    assert rc == 0
    capsys.readouterr()
    rc = main(["rule", "list", "--rules", str(rules), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["rules"][0]["armed"] is True


# --- arm requires a fresh dry-run --------------------------------------------


def test_arm_without_dry_run_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rules = tmp_path / "rules.json"
    _add_inline_rule(rules)
    capsys.readouterr()

    rc = main(["rule", "arm", "cool-when-hot", "--rules", str(rules)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "never been dry-run" in err
    assert "hint:" in err


def test_editing_a_rule_after_dry_run_blocks_arming(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], block_network: None
) -> None:
    rules = tmp_path / "rules.json"
    db = tmp_path / "sensibo.db"
    _seed_store(db)
    _add_inline_rule(rules)
    main(["rule", "dry-run", "cool-when-hot", "--rules", str(rules), "--db", str(db)])
    capsys.readouterr()

    # Edit the rule (re-add same name, different threshold value).
    rc = main(
        [
            "rule",
            "add",
            "--name",
            "cool-when-hot",
            "--pod",
            "ac1",
            "--power",
            "on",
            "--mode",
            "cool",
            "--when-location",
            "ac1",
            "--when-field",
            "temperature",
            "--when-op",
            ">",
            "--when-value",
            "30",
            "--rules",
            str(rules),
        ]
    )
    assert rc == 0
    capsys.readouterr()

    # Editing the rule invalidated its dry-run: arming is refused and points
    # the operator back at dry-run (a fresh definition must earn a fresh one).
    rc = main(["rule", "arm", "cool-when-hot", "--rules", str(rules)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "cannot arm" in err
    assert "dry-run" in err
    # and the rule is still disarmed
    rc = main(["rule", "list", "--rules", str(rules), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["rules"][0]["armed"] is False


# --- run --once with a mocked client writes exactly once ---------------------


def test_run_once_performs_the_expected_write_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rules = tmp_path / "rules.json"
    db = tmp_path / "sensibo.db"
    _seed_store(db, temp=27.0)
    _add_inline_rule(rules)
    main(["rule", "dry-run", "cool-when-hot", "--rules", str(rules), "--db", str(db)])
    main(["rule", "arm", "cool-when-hot", "--rules", str(rules)])
    capsys.readouterr()

    fake = _FakeClient({"ac1": {"on": False, "mode": "cool", "targetTemperature": 20}})
    _install_client(monkeypatch, fake)

    rc = main(["rule", "run", "--once", "--rules", str(rules), "--db", str(db), "--json"])
    assert rc == 0
    captured = capsys.readouterr()

    writes = [c for c in fake.calls if c[0] in ("patch_ac_state", "post_ac_states")]
    assert len(writes) == 1
    assert writes[0] == ("patch_ac_state", "ac1", "on", True)
    assert fake._pods["ac1"]["on"] is True

    payload = json.loads(captured.out)
    assert payload["execution"] == _EXECUTION_STR
    assert payload["outcomes"][0]["wrote"] is True
    # the action was logged to stderr, tagged with the rule name
    assert "rule 'cool-when-hot'" in captured.err


def test_run_once_with_no_armed_rules_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rules = tmp_path / "rules.json"
    db = tmp_path / "sensibo.db"
    _seed_store(db)
    _add_inline_rule(rules)  # added but NOT armed
    capsys.readouterr()

    fake = _FakeClient({"ac1": {"on": False}})
    _install_client(monkeypatch, fake)

    rc = main(["rule", "run", "--once", "--rules", str(rules), "--db", str(db)])
    assert rc == 0
    assert fake.calls == []


def test_run_once_respects_hysteresis_after_a_prior_power_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rules = tmp_path / "rules.json"
    db = tmp_path / "sensibo.db"
    _seed_store(db, temp=27.0)
    _add_inline_rule(rules)
    main(["rule", "dry-run", "cool-when-hot", "--rules", str(rules), "--db", str(db)])
    main(["rule", "arm", "cool-when-hot", "--rules", str(rules)])
    capsys.readouterr()

    fake = _FakeClient({"ac1": {"on": False, "mode": "cool", "targetTemperature": 20}})
    _install_client(monkeypatch, fake)

    # First run turns it on.
    main(["rule", "run", "--once", "--rules", str(rules), "--db", str(db)])
    # Flip the pod off out-of-band, then run again immediately: hysteresis must
    # refuse to turn it back on this soon.
    fake._pods["ac1"]["on"] = False
    capsys.readouterr()
    rc = main(["rule", "run", "--once", "--rules", str(rules), "--db", str(db), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    outcome = payload["outcomes"][0]
    assert outcome["wrote"] is False
    assert "off-time" in outcome["suppressed_reason"]
    assert fake._pods["ac1"]["on"] is False


# --- remove / disarm ---------------------------------------------------------


def test_remove_deletes_a_rule(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rules = tmp_path / "rules.json"
    _add_inline_rule(rules)
    capsys.readouterr()
    rc = main(["rule", "remove", "cool-when-hot", "--rules", str(rules)])
    assert rc == 0
    capsys.readouterr()
    rc = main(["rule", "list", "--rules", str(rules), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["rules"] == []


def test_remove_unknown_rule_is_a_user_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rules = tmp_path / "rules.json"
    rc = main(["rule", "remove", "nope", "--rules", str(rules)])
    assert rc == 1
    assert "hint:" in capsys.readouterr().err


# --- overview / explain / naming ---------------------------------------------


def test_bare_rule_prints_overview_with_local_execution(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["rule"])
    assert rc == 0
    assert _EXECUTION_STR in capsys.readouterr().out


def test_every_rule_explain_path_resolves(capsys: pytest.CaptureFixture[str]) -> None:
    for path in (
        ["rule"],
        ["rule", "overview"],
        ["rule", "list"],
        ["rule", "add"],
        ["rule", "remove"],
        ["rule", "dry-run"],
        ["rule", "arm"],
        ["rule", "disarm"],
        ["rule", "run"],
    ):
        rc = main(["explain", *path])
        assert rc == 0, f"explain {' '.join(path)} failed"
        out = capsys.readouterr().out
        assert "sensibo rule" in out


def test_rule_usage_names_the_installed_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["rule", "--help"])
    out = capsys.readouterr().out
    assert "usage: sensibo rule" in out
    assert "usage: sensibo-cli" not in out
