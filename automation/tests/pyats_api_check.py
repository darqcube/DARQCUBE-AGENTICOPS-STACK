"""Runs INSIDE darqcube/automation:local, driven by test_pyats_api.py.

Exercises the real automation API (FastAPI app, real tasks.pyats_* code,
real platforms.yml) with Starlette's TestClient. Only the two edges that need
a network are replaced:

- the inventory (tasks.get_nornir) — a fake host per device, with a platform
- the pyATS session (checks.collect) — returns Genie's OWN golden fixtures
  from automation/pyats/samples/, never hand-written Genie output

Prints one JSON object: {check name: [passed, detail]}.
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, "/app")

from fastapi.testclient import TestClient  # noqa: E402

from automation.nornir import tasks  # noqa: E402
from automation.pyats import checks  # noqa: E402
from automation.service import main  # noqa: E402

SAMPLES = Path("/app/automation/pyats/samples")
IOSXE_BGP = json.loads((SAMPLES / "iosxe-learn-bgp.json").read_text())
HVRP_BGP = json.loads((SAMPLES / "hvrp-display-bgp-peer.json").read_text())

# device -> platform, as Infrahub would report it
INVENTORY = {"router1": "ios_xe", "router2": "vrp", "mt-01": "routeros", "bgpless": "ios_xe"}
results: dict[str, list] = {}
calls: list[dict] = []


def fake_get_nornir(device=None, fresh=False):
    if device not in INVENTORY:
        raise tasks.DeviceError(f"device '{device}' is not in Infrahub")
    host = SimpleNamespace(data={"infrahub_platform": INVENTORY[device]})
    return SimpleNamespace(inventory=SimpleNamespace(hosts={device: host}))


def fake_collect(device, wanted=None):
    """What checks.collect returns, from Genie's own fixtures."""
    lock = tasks.device_lock(device)
    calls.append({"device": device, "wanted": sorted(wanted or []),
                  # RLock: _is_owned() is true only for the holding thread
                  "lock_held": lock._is_owned()})
    if device == "bgpless":
        return {}, {}, {"bgp": f"{device}: bgp is not configured"}
    data = {"router1": IOSXE_BGP, "router2": HVRP_BGP}[device]
    return {f: data for f in (wanted or [])}, {}, {}


tasks.get_nornir = fake_get_nornir
checks.collect = fake_collect
client = TestClient(main.app)


def check(name, fn):
    try:
        ok, detail = fn()
    except Exception:
        ok, detail = False, traceback.format_exc()[-600:]
    results[name] = [bool(ok), str(detail)[:600]]


def get(path):
    r = client.get(path)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, r.text


# --- /pyats/features ---------------------------------------------------------

s, body = get("/device/router1/pyats/features")
check("features_iosxe_from_platforms_yml",
      lambda: (s == 200 and body["supported"] and body["os"] == "iosxe"
               and body["features"] == {f: "learn" for f in ("interface", "platform", "bgp", "lldp")}, body))

s, body = get("/device/router2/pyats/features")
check("features_vrp_is_bgp_parse_only",
      lambda: (s == 200 and body["features"] == {"bgp": "parse"}, body))

s, body = get("/device/mt-01/pyats/features")
check("features_platform_without_genie_says_so",
      lambda: (s == 200 and body["supported"] is False and "TextFSM" in body.get("reason", ""), body))

s, body = get("/device/nosuch/pyats/features")
check("features_unknown_device_404", lambda: (s == 404, (s, body)))

# --- /pyats/learn/{feature} --------------------------------------------------

calls.clear()
s, body = get("/device/router1/pyats/learn/bgp")
check("learn_ok_returns_genie_data",
      lambda: (s == 200 and body["status"] == "ok" and body["via"] == "learn"
               and body["data"] == IOSXE_BGP, {k: body.get(k) for k in ("status", "via", "platform")}))
check("learn_collects_only_the_feature_asked_for",
      lambda: (calls and calls[-1]["wanted"] == ["bgp"], calls))
check("learn_holds_the_device_lock",
      lambda: (calls and calls[-1]["lock_held"] is True, calls))

s, body = get("/device/router2/pyats/learn/bgp")
check("learn_vrp_reports_parse", lambda: (s == 200 and body["via"] == "parse", body.get("via")))

calls.clear()
s, body = get("/device/router1/pyats/learn/ospf")
check("learn_feature_outside_allowlist_400_and_lists_choices",
      lambda: (s == 400 and "Available" in body["detail"] and "bgp" in body["detail"], body))
check("learn_outside_allowlist_never_opens_a_session", lambda: (not calls, calls))

s, body = get("/device/router2/pyats/learn/interface")
check("learn_vrp_interface_refused",
      lambda: (s == 400, (s, body)))  # Genie has no hvrp interface model

s, body = get("/device/router1/pyats/learn/Bad-Name")
check("learn_malformed_feature_400", lambda: (s == 400 and "invalid feature" in body["detail"], body))

s, body = get("/device/bad%20name/pyats/learn/bgp")
check("learn_malformed_device_400", lambda: (s == 400 and "invalid device" in body["detail"], body))

s, body = get("/device/bgpless/pyats/learn/bgp")
check("learn_absent_is_an_answer_not_ok",
      lambda: (s == 200 and body["status"] == "absent" and "data" not in body, body))

# --- /pyats/bgp ---------------------------------------------------------------

s, body = get("/device/router1/pyats/bgp")
check("bgp_iosxe_sessions_compact",
      lambda: (s == 200 and body["status"] == "ok" and "data" not in body and body["sessions"]
               and all(set(x) == {"vrf", "af", "peer", "state"} for x in body["sessions"]), body))
check("bgp_keeps_same_peer_in_different_vrfs",
      lambda: (len({(x["vrf"], x["peer"]) for x in body["sessions"]}) == len(body["sessions"])
               and len({x["vrf"] for x in body["sessions"]}) > 1, body.get("sessions")))

s, body = get("/device/router2/pyats/bgp")
check("bgp_vrp_same_shape",
      lambda: (s == 200 and body["sessions"]
               and all(set(x) == {"vrf", "af", "peer", "state"} for x in body["sessions"]), body))

s, body = get("/device/mt-01/pyats/bgp")
check("bgp_on_platform_without_genie_400", lambda: (s == 400, (s, body)))

s, body = get("/device/bgpless/pyats/bgp")
check("bgp_absent_has_no_sessions_key",
      lambda: (s == 200 and body["status"] == "absent" and "sessions" not in body, body))

print(json.dumps(results))
