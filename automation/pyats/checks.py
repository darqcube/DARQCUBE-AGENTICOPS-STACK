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


# When a feature comes back empty: is it configured at all? An empty result on
# a device without the feature (BGP on an access switch) is "does not apply",
# not an error. A match here means it IS configured and Genie returned nothing —
# a real gap, reported as an error. (os, feature) -> a command whose output is
# empty exactly when the feature is not configured.
CONFIGURED_PROBES = {
    ("iosxe", "bgp"): "show running-config | include ^router bgp",
    ("hvrp", "bgp"): "display current-configuration | include ^bgp",
}


def collect(device: str, wanted: set[str] | None = None) -> tuple[dict, dict, dict]:
    """Run the platform's declared pyATS calls for the `wanted` features.

    Returns ({feature: data}, {feature: error}, {feature: why it does not
    apply}). Only what a rule needs is
    collected: a platform declares what Genie CAN do, a rule says what is
    NEEDED, and learning the rest costs seconds per feature for nothing. An
    empty feature is an error for that feature only — one gap (IOL has no
    Genie platform model, a lab has no LLDP) must not sink an unrelated rule.
    A failed connection still raises, and every pyATS rule reports it.

    The caller must hold device_lock(device): this opens its own SSH session
    through unicon, separate from Netmiko's, and the two must not overlap on
    one device.
    """
    from automation.pyats.testbed import build_testbed

    testbed, spec = build_testbed(device)
    learn = [f for f in (spec.get("learn", []) or []) if wanted is None or f in wanted]
    parse = {f: c for f, c in (spec.get("parse", {}) or {}).items() if wanted is None or f in wanted}
    features: dict = {}
    errors: dict = {}
    absent: dict = {}
    if not learn and not parse:
        return features, errors, absent

    def nothing(feature: str, message: str) -> None:
        """File an empty result as absent (not configured) or as an error."""
        probe = CONFIGURED_PROBES.get((spec["os"], feature))
        if probe and not dev.execute(probe).strip():
            absent[feature] = f"{device}: {feature} is not configured (nothing matches '{probe}')"
        else:
            errors[feature] = message

    dev = testbed.devices[device]
    dev.connect(log_stdout=False, learn_hostname=True)
    try:
        for feature in learn:
            learned = dev.learn(feature)
            info = getattr(learned, "info", None)
            if info:
                features[feature] = info
            else:
                nothing(feature,
                        f"{device}: learn('{feature}') returned nothing although it is "
                        f"configured — Genie may have no '{feature}' model for os "
                        f"'{spec['os']}' on this device")
        for feature, command in parse.items():
            try:
                parsed = dev.parse(command)
            except Exception as exc:
                # Genie raises SchemaEmptyParserError when the command produced
                # no parsable output — the same "nothing" as above.
                nothing(feature, f"{device}: parse('{command}') returned nothing: {exc}")
                continue
            if parsed:
                features[feature] = parsed
            else:
                nothing(feature, f"{device}: parse('{command}') returned nothing")
    finally:
        dev.disconnect()
    return features, errors, absent


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
