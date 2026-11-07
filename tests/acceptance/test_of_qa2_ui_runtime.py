"""Tester (QA) cycle 2, Sprint 02a: adversarial probes of the BUG-1 fix (drawPositions).

The fix keeps ONE positions table in the page at a time: drawing one container clears the other
when it holds inputs. The owner's price boxes live in those tables, so the questions here are
what the clearing can take away from the owner:

* a price typed into the visible table, when a render for the hidden one lands (an alert
  reload that was already in flight when the owner switched tab);
* the Monitoring table, when the alerts cannot be loaded after a visit to the Live Desk;
* the one-id-per-page promise itself, through a round trip of tab switches.

Same rig as test_of_qa_ui_runtime.py: app.js is loaded whole into node, the payloads are the
real Desk's. Skipped (with a reason) on a machine without node.
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import test_of_qa_ui_runtime as U1
import us001_helpers as H
from test_us002_b_desk_end_to_end import Rig

from onelot.config import AppConfig

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

# the cycle-1 harness, plus: real tab switches (the hashchange handler app.js registers), a
# `burst` of steps that runs without letting promises settle between them (an owner who is
# faster than a fetch), and GET paths that fail.
HARNESS2 = U1.HARNESS
_OLD_LISTENER = "g.addEventListener = () => {};"
_NEW_LISTENER = (
    "let hashHandler = null;\n"
    'g.addEventListener = (t, fn) => { if (t === "hashchange") hashHandler = fn; };'
)
assert _OLD_LISTENER in HARNESS2
HARNESS2 = HARNESS2.replace(_OLD_LISTENER, _NEW_LISTENER, 1)
_OLD_TAB = 'else if (step.op === "tab") { activeTab = step.name; }'
_NEW_TAB = r"""else if (step.op === "tab") { activeTab = step.name; }
      else if (step.op === "goto") { g.location.hash = "#" + step.name; hashHandler(); }
      else if (step.op === "failNext") { scenario.fail = step.paths; }
      else if (step.op === "burst") {
        for (const sub of step.ops) {
          if (sub.op === "stream") { skew += 5000; stream.onmessage({ data: JSON.stringify(sub.payload) }); }
          else if (sub.op === "goto") { g.location.hash = "#" + sub.name; hashHandler(); }
          else if (sub.op === "setIn") {
            const k = byId(sub.container).querySelectorAll("input[id]").find((x) => x.id === sub.id);
            if (k) { k.value = sub.value; k.dataset.dirty = "1"; }
          }
        }
      }"""
assert _OLD_TAB in HARNESS2
HARNESS2 = HARNESS2.replace(_OLD_TAB, _NEW_TAB, 1)
_OLD_INPUT = "function inputFrom(tag, id) {"
_NEW_INPUT = (
    'Object.defineProperty(Elem.prototype, "firstElementChild", { get() { return new Elem(""); } });\n'
    "Elem.prototype.replaceWith = function () {};\n" + _OLD_INPUT
)
assert _OLD_INPUT in HARNESS2
HARNESS2 = HARNESS2.replace(_OLD_INPUT, _NEW_INPUT, 1)
_OLD_FETCH = "  fetches.push({ url, method, body });"
_NEW_FETCH = """  fetches.push({ url, method, body });
  if ((scenario.fail || []).some((p) => url.startsWith(p))) {
    return { ok: false, status: 500, json: async () => ({ detail: "boom" }) };
  }"""
assert _OLD_FETCH in HARNESS2
HARNESS2 = HARNESS2.replace(_OLD_FETCH, _NEW_FETCH, 1)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    H.block_network(monkeypatch)


@pytest.fixture
def app_cfg():
    return AppConfig.load()


@pytest.fixture
def desk_db(tmp_path):
    d = H.new_db(tmp_path, "desk.db", make_global=True)
    yield d
    H.dispose_db(d, was_global=True)


@pytest.fixture
def rig(app_cfg, desk_db):
    return Rig(app_cfg, desk_db)


def run_ui2(tmp_path: Path, scenario: dict) -> dict:
    harness = tmp_path / "harness2.js"
    harness.write_text(HARNESS2, encoding="utf-8")
    spec = tmp_path / "scenario2.json"
    spec.write_text(json.dumps(scenario, default=str), encoding="utf-8")
    done = subprocess.run(
        ["node", str(harness), str(U1.APP_JS), str(U1.INDEX_HTML), str(spec)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


GET = {"/api/desk/signals": U1.signals_payload([]), "/api/desk/journal": {"trades": []}}


def streams(rig):
    pid, alert, first, second = U1.TestPositionTables.streams(rig)
    third = copy.deepcopy(second)
    third["state"]["desk"]["version"] += 1
    for p in third["state"]["desk"]["positions"]:
        p["unrealized"] = (p.get("unrealized") or 0) + 3.0  # a table that is drawn again
    return pid, alert, first, second, third


class TestOneTablePerPage:
    def test_a_round_trip_of_tab_switches_never_repeats_an_id_and_always_draws_the_visible_table(
        self, rig, tmp_path
    ):
        pid, _, first, second, third = streams(rig)
        ids = {"op": "ids", "containers": ["home-positions", "desk-positions"]}
        res = run_ui2(
            tmp_path,
            {
                "get": GET,
                "steps": [
                    {"op": "stream", "payload": first},  # Home is the tab shown
                    {"op": "count", "id": "home-positions", "needle": f'id="dx_{pid}"'},  # 1
                    {"op": "goto", "name": "live"},
                    {"op": "stream", "payload": second},
                    {"op": "count", "id": "desk-positions", "needle": f'id="dx_{pid}"'},  # 4
                    ids,  # 5
                    {"op": "goto", "name": "home"},
                    {"op": "count", "id": "home-positions", "needle": f'id="dx_{pid}"'},  # 7
                    ids,  # 8
                    {"op": "goto", "name": "live"},
                    {"op": "stream", "payload": third},
                    {"op": "count", "id": "desk-positions", "needle": f'id="dx_{pid}"'},  # 11
                    ids,  # 12
                ],
            },
        )
        assert res["errors"] == []
        steps = res["steps"]
        assert steps[1]["n"] == 1 and steps[4]["n"] == 1
        assert steps[7]["n"] == 1, "back on Home the Monitoring table is drawn again"
        assert steps[11]["n"] == 1, "back on the Live Desk its table is drawn again"
        assert [steps[i]["duplicated"] for i in (5, 8, 12)] == [[], [], []]

    def test_a_price_typed_on_the_live_row_survives_the_next_poll_after_a_visit_to_home(
        self, rig, tmp_path
    ):
        pid, _, first, second, third = streams(rig)
        res = run_ui2(
            tmp_path,
            {
                "get": GET,
                "steps": [
                    {"op": "stream", "payload": first},
                    {"op": "goto", "name": "live"},
                    {"op": "stream", "payload": second},
                    {"op": "goto", "name": "home"},
                    {"op": "goto", "name": "live"},
                    {
                        "op": "setIn",
                        "container": "desk-positions",
                        "id": f"dx_{pid}",
                        "value": "55.5",
                    },
                    {"op": "stream", "payload": third},
                    {"op": "getIn", "container": "desk-positions", "id": f"dx_{pid}"},  # 7
                ],
            },
        )
        assert res["errors"] == []
        assert res["steps"][7]["value"] == "55.5"


class TestWhatTheClearingCanTakeAway:
    def test_a_home_reload_already_in_flight_cannot_wipe_a_price_typed_on_the_live_desk(
        self, rig, tmp_path
    ):
        """The alerts reload every 15 s while Home is shown. If the owner has just switched to
        the Live Desk when that fetch lands, its render of the Home table must not clear the
        Live table they are typing into."""
        pid, _, first, second, third = streams(rig)

        def at(version):  # a snapshot that is new every time (Home redraws, reloads at 15 s)
            m = copy.deepcopy(second)
            m["state"]["desk"]["version"] = version
            return {"op": "stream", "payload": m}

        base = first["state"]["desk"]["version"]
        res = run_ui2(
            tmp_path,
            {
                "get": GET,
                "steps": [
                    {"op": "stream", "payload": first},  # the first reload starts and lands
                    at(base + 1),
                    at(base + 2),
                    {
                        "op": "burst",  # one breath: a reload starts, the owner moves and types
                        "ops": [
                            {"op": "stream", "payload": at(base + 3)["payload"]},
                            {"op": "stream", "payload": at(base + 4)["payload"]},
                            {"op": "goto", "name": "live"},
                            {
                                "op": "setIn",
                                "container": "desk-positions",
                                "id": f"dx_{pid}",
                                "value": "987.65",
                            },
                        ],
                    },
                    {"op": "getIn", "container": "desk-positions", "id": f"dx_{pid}"},  # 4
                ],
            },
        )
        assert res["errors"] == []
        reloads = [f for f in res["fetches"] if f["url"].startswith("/api/desk/signals")]
        assert len(reloads) >= 3, "the harness never started the reload inside the burst"
        assert res["steps"][4]["found"] and res["steps"][4]["value"] == "987.65"

    def test_the_monitoring_table_still_lists_the_position_when_the_alerts_cannot_be_loaded(
        self, rig, tmp_path
    ):
        """After a visit to the Live Desk the Home table was cleared. Coming back, the alerts
        fetch fails (the desk is booting, a 500): Monitoring must not stay blank for a
        position the snapshot in hand still lists."""
        pid, _, first, second, _ = streams(rig)
        res = run_ui2(
            tmp_path,
            {
                "get": GET,
                "fail": [],
                "steps": [
                    {"op": "stream", "payload": first},
                    {"op": "goto", "name": "live"},
                    {"op": "stream", "payload": second},
                    {"op": "failNext", "paths": ["/api/desk/signals"]},
                    {"op": "goto", "name": "home"},
                    {"op": "count", "id": "home-positions", "needle": f'id="dx_{pid}"'},  # 5
                ],
            },
        )
        assert res["errors"] == []
        assert res["steps"][5]["n"] == 1

    def test_a_price_typed_on_the_live_row_is_kept_when_the_owner_looks_at_home_and_comes_back(
        self, rig, tmp_path
    ):
        """keepInputs() exists so a redraw never wipes a price being typed; a tab switch that
        clears the hidden table does (the text is typed, not yet saved)."""
        pid, _, first, second, _ = streams(rig)
        res = run_ui2(
            tmp_path,
            {
                "get": GET,
                "steps": [
                    {"op": "stream", "payload": first},
                    {"op": "goto", "name": "live"},
                    {"op": "stream", "payload": second},
                    {
                        "op": "setIn",
                        "container": "desk-positions",
                        "id": f"dx_{pid}",
                        "value": "55.5",
                    },
                    {"op": "goto", "name": "home"},
                    {"op": "goto", "name": "live"},
                    {"op": "getIn", "container": "desk-positions", "id": f"dx_{pid}"},  # 6
                ],
            },
        )
        assert res["errors"] == []
        assert res["steps"][6]["value"] == "55.5"
