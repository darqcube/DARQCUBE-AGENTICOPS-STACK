#!/usr/bin/env python3
"""Apply source-of-truth/devices/*.yml to Infrahub.

Those files are the deployment's own inventory and are gitignored; the repo
ships only source-of-truth/devices/examples/, which is never read.

Idempotent: re-running updates existing nodes instead of duplicating them, so
this is the normal way to change a device, not just to create one.

Schema-driven. The fields a record may carry are read from the schema loaded
into Infrahub (`make schema`), not from a list in this file, so a new attribute
in darqcube.yml is accepted here with no code change. A key the schema does not
know is an ERROR: dropping it would report success while Infrahub never got
the value — the typo `enviroment: demo` must fail, not vanish.

Every file is validated before anything is written, so one bad record among
400 changes nothing rather than leaving a half-applied fleet.

The YAML is the whole record. A field left out is reset to its schema default
(or cleared), and a device's tags are set to exactly the list given — except
the OBSERVED fields below (SSH host keys), which a record may leave out. A value
edited in the Infrahub UI is therefore overwritten by the next seed — see
docs/administration/infrahub-guide.md for which of the two owns what.

Runs inside the infrahub-server container (which already has the SDK):
    make seed
"""
from __future__ import annotations

import ipaddress
import os
import sys
from pathlib import Path

import yaml
from infrahub_sdk import Config, InfrahubClientSync

INFRAHUB_URL = os.environ.get("INFRAHUB_ADDRESS", "http://infrahub-server:8000")
TOKEN = os.environ.get("INFRAHUB_API_TOKEN") or os.environ.get("INFRAHUB_INITIAL_ADMIN_TOKEN")
BRANCH = os.environ.get("INFRAHUB_BRANCH", "main")

DEVICES_DIR = os.environ.get("DEVICES_DIR", "/devices")
PLATFORMS_FILE = os.environ.get("PLATFORMS_FILE", "/platforms.yml")

# Top-level YAML key -> Infrahub kind, in dependency order: a device refers to
# its site and tags by name, so both must exist before the device is written.
# This is the one list here that a schema change can touch — and only when a
# whole new node type is added, not a field.
SECTIONS = (
    ("tags", "BuiltinTag"),
    ("sites", "NetworkSite"),
    ("devices", "NetworkDevice"),
    # What the network serves (optional; docs/how-to/model-applications.md).
    # A prefix may name its gateway device, a host its prefix, a service its
    # host and application — so each comes after what it refers to.
    ("prefixes", "NetworkPrefix"),
    ("hosts", "NetworkHost"),
    ("applications", "NetworkApplication"),
    ("services", "NetworkService"),
)

# Attributes that record what was OBSERVED on the device rather than intent —
# written by operations (`make pin-host-keys`), not by the YAML. A record that
# leaves one out keeps the value in Infrahub instead of resetting it, or every
# seed would silently un-pin the fleet's SSH host keys. A record that sets one
# (keys provisioned out of band) still wins.
OBSERVED = {"NetworkDevice": ("ssh_host_keys",)}

# Relationships a record may set. Component, Parent, Group and Profile
# relationships are managed from the other side, or by Infrahub itself.
SETTABLE_RELATIONSHIPS = {"Attribute", "Generic"}

# What a YAML value must look like for each attribute kind. Checked before any
# write, because Infrahub rejects a bad value only when that node is saved —
# halfway through the run.
_TEXT = (str,)
VALUE_TYPES = {
    "Text": _TEXT,
    "TextArea": _TEXT,
    "Dropdown": _TEXT,
    "Boolean": (bool,),
    "Number": (int,),
}


def load(path: str | Path) -> dict:
    with open(path) as fh:
        return yaml.safe_load(fh) or {}


# --- reading the YAML ------------------------------------------------------

def inventory_files(directory: str | Path) -> list[Path]:
    """The deployment's own *.yml, top level only.

    Top level only is what keeps examples/ out: the shipped examples must
    never be seeded, or every fresh install starts with made-up devices that
    the collectors then poll.
    """
    directory = Path(directory)
    return sorted(directory.glob("*.yml")) + sorted(directory.glob("*.yaml"))


def load_records(directory: str | Path) -> tuple[dict[str, list], list[str]]:
    """Every record in every *.yml under `directory`, grouped by section.

    Split the fleet across as many files as is convenient — one per region or
    site reviews far better than one 400-entry file. Returns
    ({section: [(filename, record), ...]}, errors).
    """
    sections: dict[str, list] = {name: [] for name, _ in SECTIONS}
    seen: dict[tuple[str, str], str] = {}
    errors: list[str] = []

    for path in inventory_files(directory):
        doc = load(path)
        if not isinstance(doc, dict):
            errors.append(f"{path.name}: expected top-level keys {', '.join(sections)}")
            continue
        for key, items in doc.items():
            if key not in sections:
                errors.append(f"{path.name}: unknown top-level key '{key}' (have: {', '.join(sections)})")
                continue
            for rec in items or []:
                if not isinstance(rec, dict) or not rec.get("name"):
                    errors.append(f"{path.name}: every entry under '{key}' needs a name")
                    continue
                name = str(rec["name"])
                if (key, name) in seen:
                    errors.append(f"{path.name}: {key} '{name}' is also defined in {seen[(key, name)]}")
                    continue
                seen[(key, name)] = path.name
                sections[key].append((path.name, rec))
    return sections, errors


# --- reading the schema ----------------------------------------------------

def schema_view(client, kind: str) -> dict:
    """The parts of one kind's loaded schema that decide what a record may hold."""
    schema = client.schema.get(kind=kind, branch=BRANCH)
    attributes = {}
    for attr in schema.attributes:
        if attr.read_only:
            continue
        choices = [c["name"] if isinstance(c, dict) else c.name for c in (attr.choices or [])]
        attributes[attr.name] = {
            "kind": attr.kind,
            "optional": attr.optional,
            "default": attr.default_value,
            "choices": choices,
        }
    relationships = {
        rel.name: {"peer": rel.peer, "cardinality": rel.cardinality, "optional": rel.optional}
        for rel in schema.relationships
        if rel.kind in SETTABLE_RELATIONSHIPS and not rel.read_only
    }
    return {"attributes": attributes, "relationships": relationships}


# --- validation (pure — no Infrahub, so it is testable offline) ------------

def _value_error(attr: dict, value) -> str | None:
    if value is None:
        return None
    if attr["kind"] == "IPHost":
        try:
            ipaddress.ip_interface(str(value))
        except ValueError:
            return f"'{value}' is not an IP address"
        return None
    if attr["kind"] == "IPNetwork":
        # strict: 10.0.1.5/24 is a typo for a host or a prefix, never a prefix.
        try:
            ipaddress.ip_network(str(value), strict=True)
        except ValueError:
            return f"'{value}' is not a network in CIDR form (e.g. 10.0.1.0/24)"
        return None
    expected = VALUE_TYPES.get(attr["kind"])
    # bool is a subclass of int: `true` must not pass as a Number.
    if expected and (not isinstance(value, expected) or (expected == (int,) and isinstance(value, bool))):
        return f"'{value}' is not a valid {attr['kind']}"
    if attr["choices"] and value not in attr["choices"]:
        return f"'{value}' is not one of: {', '.join(attr['choices'])}"
    return None


def check_device(rec: dict, platforms: dict) -> list[str]:
    """Rules the schema cannot express, specific to devices."""
    problems = []
    platform = rec.get("platform")
    if platform is not None and platform not in platforms:
        problems.append(
            f"platform '{platform}' is not in platforms.yml (have: {', '.join(sorted(platforms))})"
        )
    if not rec.get("management_ip") and not rec.get("management_host"):
        problems.append("needs management_ip or management_host — nothing to poll or SSH to")
    return problems


def check_service(rec: dict) -> list[str]:
    """A service is an endpoint flows are matched against: it needs a real port."""
    port = rec.get("port")
    if port is None:
        return ["needs a port"]
    if isinstance(port, int) and not isinstance(port, bool) and not 1 <= port <= 65535:
        return [f"port {port} is outside 1-65535"]
    return []


def check_prefix(rec: dict) -> list[str]:
    return [] if rec.get("prefix") else ["needs a prefix, e.g. 10.0.1.0/24"]


def check_relations(sections: dict) -> list[str]:
    """Rules that span records, so they cannot live on a single one.

    - Two services on one host with the same protocol and port would make a
      flow match two applications.
    - A host that runs services needs an address, or nothing can be matched
      to it.
    - A host's address must fall inside the prefix it says it lives in.
    """
    errors: list[str] = []
    hosts = {str(r["name"]): (src, r) for src, r in sections.get("hosts", [])}
    prefixes = {str(r["name"]): r for _, r in sections.get("prefixes", [])}

    seen: dict[tuple, str] = {}
    for src, rec in sections.get("services", []):
        key = (rec.get("host"), rec.get("protocol", "tcp"), rec.get("port"))
        if key in seen:
            errors.append(
                f"{src}: service '{rec['name']}' uses {key[1]}/{key[2]} on host '{key[0]}', "
                f"already used by service '{seen[key]}'"
            )
        else:
            seen[key] = rec["name"]
        host = hosts.get(str(rec.get("host")))
        if host and not host[1].get("address"):
            errors.append(f"{host[0]}: host '{rec['host']}' runs service '{rec['name']}' but has no address")

    for name, (src, rec) in hosts.items():
        prefix = prefixes.get(str(rec.get("prefix")))
        if not (prefix and rec.get("address") and prefix.get("prefix")):
            continue
        try:
            inside = ipaddress.ip_interface(str(rec["address"])).ip in ipaddress.ip_network(str(prefix["prefix"]))
        except ValueError:
            continue  # the per-record check already reported the bad value
        if not inside:
            errors.append(f"{src}: host '{name}' address {rec['address']} is not inside prefix "
                          f"'{rec['prefix']}' ({prefix['prefix']})")
    return errors


def validate(sections: dict, views: dict, known: dict, platforms: dict) -> list[str]:
    """Every problem in every record. Empty means safe to write.

    known: {kind: set of names} — what already exists in Infrahub plus what
    these files define, so a device may refer to a site created in the UI or
    one defined three files over.
    """
    errors: list[str] = []
    section_of = {kind: name for name, kind in SECTIONS}

    for section, kind in SECTIONS:
        view = views[kind]
        attrs, rels = view["attributes"], view["relationships"]
        for src, rec in sections[section]:
            where = f"{src}: {section[:-1]} '{rec['name']}'"

            for key in rec:
                if key not in attrs and key not in rels:
                    errors.append(
                        f"{where}: unknown field '{key}' — not in the {kind} schema "
                        f"(have: {', '.join(sorted([*attrs, *rels]))})"
                    )

            for name, attr in attrs.items():
                if name not in rec:
                    if not attr["optional"] and attr["default"] is None:
                        errors.append(f"{where}: missing required field '{name}'")
                    continue
                problem = _value_error(attr, rec[name])
                if problem:
                    errors.append(f"{where}: {name} {problem}")

            for name, rel in rels.items():
                value = rec.get(name)
                if value is None:
                    if not rel["optional"]:
                        errors.append(f"{where}: missing required field '{name}'")
                    continue
                many = rel["cardinality"] == "many"
                if many and not isinstance(value, list):
                    errors.append(f"{where}: {name} must be a list")
                    continue
                if not many and not isinstance(value, str):
                    errors.append(f"{where}: {name} must be a single name")
                    continue
                for peer in value if many else [value]:
                    if peer not in known.get(rel["peer"], set()):
                        home = section_of.get(rel["peer"])
                        hint = f" — add it under '{home}:'" if home else ""
                        errors.append(f"{where}: {name} '{peer}' does not exist{hint}")

            if kind == "NetworkDevice":
                errors.extend(f"{where}: {p}" for p in check_device(rec, platforms))
            elif kind == "NetworkService":
                errors.extend(f"{where}: {p}" for p in check_service(rec))
            elif kind == "NetworkPrefix":
                errors.extend(f"{where}: {p}" for p in check_prefix(rec))
    errors.extend(check_relations(sections))
    return errors


# --- writing ---------------------------------------------------------------

def _sync_many(node, rel_name: str, wanted: set[str]) -> None:
    """Make a cardinality-many relationship hold exactly `wanted`.

    Assigning a list does not work: the SDK only intercepts assignment for
    cardinality-one relationships, so a list would replace the manager object
    and the save would send nothing.
    """
    manager = getattr(node, rel_name)
    if not manager.initialized:
        manager.fetch()
    current = set(manager.peer_ids)
    for peer_id in current - wanted:
        manager.remove(peer_id)
    for peer_id in wanted - current:
        manager.add(peer_id)


def _peer_ids(rel: dict, value, ids: dict):
    """Names in the YAML -> Infrahub ids, one or a list, per cardinality."""
    if rel["cardinality"] == "many":
        return [ids[rel["peer"]][v] for v in value]
    return ids[rel["peer"]][value]


def apply(client, sections: dict, views: dict, existing: dict, ids: dict) -> dict[str, int]:
    """Write every record. Returns {section: count}."""
    counts = {}
    for section, kind in SECTIONS:
        view = views[kind]
        attrs, rels = view["attributes"], view["relationships"]
        for _, rec in sections[section]:
            name = str(rec["name"])

            def peer_ids(rel_name, rec=rec, rels=rels):
                return _peer_ids(rels[rel_name], rec[rel_name], ids)

            node = existing[kind].get(name)
            if node is None:
                data = {k: v for k, v in rec.items() if k in attrs}
                data.update({r: peer_ids(r) for r in rels if rec.get(r) is not None})
                node = client.create(kind=kind, branch=BRANCH, **data)
                node.save()
                ids[kind][name] = node.id
                action = "created"
            else:
                for attr_name, attr in attrs.items():
                    if attr_name == "name":
                        continue
                    if attr_name in OBSERVED.get(kind, ()) and attr_name not in rec:
                        continue   # kept: operations owns it unless the YAML says
                    getattr(node, attr_name).value = rec.get(attr_name, attr["default"])
                for rel_name, rel in rels.items():
                    if rel["cardinality"] == "many":
                        _sync_many(node, rel_name, set(peer_ids(rel_name)) if rec.get(rel_name) else set())
                    elif rec.get(rel_name) is not None:
                        setattr(node, rel_name, peer_ids(rel_name))
                node.save()
                action = "updated"
            print(f"  {section[:-1]:<12}{name:<24}{action}")
        counts[section] = len(sections[section])
    return counts


def main() -> int:
    if not TOKEN:
        print("!! INFRAHUB_API_TOKEN not set", file=sys.stderr)
        return 2

    # A fresh clone has no inventory — the repo ships only examples/. That is
    # the normal first-install state, not a failure.
    if not inventory_files(DEVICES_DIR):
        print("no device files yet — nothing to seed. Start from the examples:")
        print("    cp source-of-truth/devices/examples/*.yml source-of-truth/devices/")
        return 0

    sections, errors = load_records(DEVICES_DIR)
    if errors:
        return report(errors)

    platforms = load(PLATFORMS_FILE)
    client = InfrahubClientSync(address=INFRAHUB_URL, config=Config(api_token=TOKEN))

    # `make seed BRANCH=x` stages the change on an Infrahub branch for review.
    # Created on first use, so a batch needs no separate UI step before it.
    branches = client.branch.all()
    if BRANCH not in branches:
        client.branch.create(branch_name=BRANCH, description="Staged by make seed")
        print(f"created Infrahub branch '{BRANCH}'")
    else:
        # A merged branch is read-only: the first save would fail with a
        # GraphQL traceback. Refuse before writing, and say what to do.
        status = getattr(branches[BRANCH].status, "value", branches[BRANCH].status)
        if str(status).upper() != "OPEN":
            return report([
                f"Infrahub branch '{BRANCH}' is {str(status).lower()} and read-only — "
                f"each change needs a new branch, e.g.: make seed BRANCH={BRANCH}-2"
            ])

    kinds = {kind for _, kind in SECTIONS}
    try:
        views = {kind: schema_view(client, kind) for kind in kinds}
    except Exception as exc:  # SchemaNotFoundError, and whatever an older SDK raises instead
        print(f"!! cannot read the schema from Infrahub ({exc}) — run: make schema", file=sys.stderr)
        return 1
    kinds |= {rel["peer"] for view in views.values() for rel in view["relationships"].values()}

    # One query per kind, not one per record: 400 filter calls is minutes.
    existing, ids, known = {}, {}, {}
    for kind in kinds:
        many = [r for r, rel in views.get(kind, {}).get("relationships", {}).items()
                if rel["cardinality"] == "many"]
        nodes = client.all(kind=kind, branch=BRANCH, include=many or None)
        existing[kind] = {n.name.value: n for n in nodes}
        ids[kind] = {name: n.id for name, n in existing[kind].items()}
        known[kind] = set(ids[kind])
    for section, kind in SECTIONS:
        known[kind] |= {str(rec["name"]) for _, rec in sections[section]}

    errors = validate(sections, views, known, platforms)
    if errors:
        return report(errors)

    print(f"Seeding Infrahub branch '{BRANCH}' from {DEVICES_DIR}:")
    counts = apply(client, sections, views, existing, ids)
    summary = ", ".join(f"{n} {section}" for section, n in counts.items())
    print(f"\nseeded {summary} — now run: make render")
    return 0


def report(errors: list[str]) -> int:
    print("!! nothing was written — fix these first:", file=sys.stderr)
    for err in errors:
        print(f"   {err}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
