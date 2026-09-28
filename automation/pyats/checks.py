"""pyATS / Genie collection, and the evaluation of what it returns.

Two halves, kept apart on purpose:

    collect()      opens a pyATS session and runs the learn()/parse() calls a
                   platform declares in platforms.yml. Needs a device.
    bgp_peers()    pure functions over Genie's output. Tested offline against
                   Genie's own golden fixtures in samples/.

Genie's BGP shape differs by OS, and both are real:

    iosxe  learn("bgp")         ...["neighbor"][<ip>]["session_state"]
    hvrp   parse("display bgp peer")  ...["peer"][<ip>]["state"]
"""
from __future__ import annotations

ESTABLISHED = "established"


class PyatsEmpty(RuntimeError):
    """A learn()/parse() returned nothing.

    Raised rather than returning {} because an empty result and "nothing to
    report" are indistinguishable — the same reasoning as the TextFSM layer.
    A learn() for a feature with no model on this OS connects successfully
    and returns nothing, which would otherwise read as a clean pass.
    """


def collect(device: str) -> dict:
    """Run the platform's declared pyATS calls. Returns {feature: data}.

    The caller must hold device_lock(device): this opens its own SSH session
    through unicon, separate from Netmiko's, and the two must not overlap on
    one device.
    """
    from automation.pyats.testbed import build_testbed

    testbed, spec = build_testbed(device)
    dev = testbed.devices[device]
    dev.connect(log_stdout=False, learn_hostname=True)
    features: dict = {}
    try:
        for feature in spec.get("learn", []) or []:
            learned = dev.learn(feature)
            info = getattr(learned, "info", None)
            if not info:
                raise PyatsEmpty(
                    f"{device}: learn('{feature}') returned nothing. Either the "
                    f"feature is not configured, or Genie has no '{feature}' model "
                    f"for os '{spec['os']}' — check the pyats block in platforms.yml."
                )
            features[feature] = info
        for feature, command in (spec.get("parse", {}) or {}).items():
            try:
                parsed = dev.parse(command)
            except Exception as exc:
                # Genie raises SchemaEmptyParserError when the command produced
                # no parsable output — the same "nothing" as above.
                raise PyatsEmpty(f"{device}: parse('{command}') returned nothing: {exc}") from exc
            if not parsed:
                raise PyatsEmpty(f"{device}: parse('{command}') returned nothing")
            features[feature] = parsed
    finally:
        dev.disconnect()
    return features


def bgp_peers(tree) -> list[dict]:
    """Every BGP session in a Genie result: {vrf, af, peer, state}.

    A SESSION is identified by its context, not just the neighbor address.
    The same peer IP routinely has separate sessions in several VRFs — Genie's
    own iosxe fixture has 2.2.2.2 in both VRF1 and default. Collapsing by IP
    would let a session that is DOWN in one VRF hide behind the same address
    being UP in another.

    Walks to any mapping named `neighbor` (iosxe) or `peer` (hvrp), recording
    the `vrf` and `address_family` keys passed through on the way. It does not
    descend INTO a peer once found — iosxe also nests a transport
    `connection.state` that would otherwise be counted as a second session.
    """
    found: list[dict] = []

    def walk(node, ctx):
        if not isinstance(node, dict):
            return
        for key, child in node.items():
            if key in ("neighbor", "peer") and isinstance(child, dict):
                for peer, attrs in child.items():
                    if isinstance(attrs, dict):
                        state = attrs.get("session_state") or attrs.get("state")
                        if state is not None:
                            found.append({**ctx, "peer": str(peer), "state": str(state)})
            elif key in ("vrf", "address_family") and isinstance(child, dict):
                # The CHILD keys are the names: vrf > VRF1, address_family > ipv4.
                field = "vrf" if key == "vrf" else "af"
                for name, sub in child.items():
                    walk(sub, {**ctx, field: str(name)})
            else:
                walk(child, ctx)

    walk(tree, {"vrf": "default", "af": ""})
    return found


def session_label(session: dict) -> str:
    """Human-readable: `VRF1 2.2.2.2`, `default/ipv4 5.5.5.5`."""
    ctx = session["vrf"] + (f"/{session['af']}" if session.get("af") else "")
    return f"{ctx} {session['peer']}"


def check_bgp_established(data, rule: dict) -> list[dict]:
    """Failures for every BGP session that is not Established."""
    sessions = bgp_peers(data)
    if not sessions:
        # BGP was collected but no sessions found — the device may simply run
        # no BGP. Nothing to fail.
        return []
    # ignore_peers may name a bare address (every VRF) or `VRF address`.
    ignore = set(rule.get("ignore_peers", []) or [])
    return [
        {"interface": session_label(s),
         "detail": f"BGP session is {s['state']}, not Established"}
        for s in sessions
        if s["state"].lower() != ESTABLISHED
        and s["peer"] not in ignore
        and session_label(s) not in ignore
    ]


# Checks over pyATS features, keyed by the `check:` name in rules.yml.
CHECKS = {
    "bgp_peers_established": check_bgp_established,
}
