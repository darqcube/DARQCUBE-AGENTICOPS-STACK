"""The automation API — get and put information and configuration on devices.

Every device operation in the stack goes through here, including the ones an AI
platform calls: mcp-netmiko and mcp-assurance front THIS API rather than opening
their own SSH sessions. That keeps credentials, TextFSM parsing and error
handling in exactly one place.

The same functions are importable by the scripts in automation/netmiko/, so
nothing is duplicated between CLI and HTTP.
"""
from __future__ import annotations

import re

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

# Absolute imports only — automation/nornir/ and automation/textfsm/ shadow the
# installed libraries if this package is imported any other way.
from automation.assurance import normalise
from automation.nornir import tasks
from automation.textfsm import parse as textfsm_parse
from automation.ttp import parse as ttp_parse

app = FastAPI(
    title="DarqCube Automation",
    description="Get and put information and configuration on network devices.",
    version="1.0",
)

# Device names come from Infrahub, but a name still reaches this API from an
# untrusted caller — an AI platform through mcp-netmiko, for instance — and is
# used to build CLI commands. Bound it before it goes anywhere near a device.
DEVICE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def check_device_name(device: str) -> str:
    if not DEVICE_RE.fullmatch(device):
        raise HTTPException(400, f"invalid device name: {device!r}")
    return device


class ConfigPush(BaseModel):
    lines: list[str] = Field(..., min_length=1, description="Configuration lines to send")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/devices")
def devices():
    """Every device Infrahub knows about, as the automation layer sees it."""
    try:
        return {"devices": tasks.list_devices()}
    except Exception as exc:
        raise HTTPException(502, f"inventory unavailable: {exc}") from exc


@app.get("/device/{device}/config")
def device_config(device: str):
    """Fetch the running configuration. A copy is archived under configs/."""
    check_device_name(device)
    try:
        return tasks.get_config(device)
    except tasks.DeviceError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"{device}: {exc}") from exc


@app.get("/device/{device}/state")
def device_state(
    device: str,
    command: str | None = Query(None, description="Override the platform's default state command"),
    raw: bool = Query(False, description="Return unparsed CLI output"),
):
    """Operational state, parsed into rows by TextFSM.

    `raw=1` returns the unparsed output — that is how you capture a new sample
    for automation/textfsm/samples/ when a template needs writing.
    """
    check_device_name(device)
    try:
        return tasks.get_state(device, command=command, raw=raw)
    except tasks.DeviceError as exc:
        raise HTTPException(404, str(exc)) from exc
    except textfsm_parse.ParseError as exc:
        # 422, not 500: the device answered, we could not parse it. The message
        # names the template gap and points at the runbook.
        raise HTTPException(
            422,
            f"{exc} — capture the output with ?raw=1 and see docs/how-to/add-a-textfsm-template.md",
        ) from exc
    except Exception as exc:
        raise HTTPException(502, f"{device}: {exc}") from exc


@app.post("/device/{device}/config")
def device_config_push(device: str, body: ConfigPush):
    """Push configuration lines. The previous config is archived first."""
    check_device_name(device)
    try:
        return tasks.put_config(device, body.lines)
    except tasks.DeviceError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"{device}: {exc}") from exc


@app.post("/device/{device}/check")
def device_check(device: str):
    """Run the assurance rules against a device.

    Vendor-neutral: the same rules in automation/assurance/rules.yml run for
    Cisco, Huawei and MikroTik, because parsed output is normalised to one
    shape first. Add a rule by editing that file — no code change.
    """
    check_device_name(device)
    try:
        return tasks.run_assurance(device)
    except tasks.DeviceError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (textfsm_parse.ParseError, normalise.NormaliseError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"{device}: {exc}") from exc


@app.get("/device/{device}/snapshot")
def device_snapshot(device: str):
    """A comparable point-in-time view of a device, for pre/post comparison."""
    check_device_name(device)
    try:
        return tasks.snapshot_device(device)
    except tasks.DeviceError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (textfsm_parse.ParseError, normalise.NormaliseError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"{device}: {exc}") from exc


@app.get("/device/{device}/config/structured")
def device_config_structured(device: str, kind: str = Query("interfaces")):
    """Running config parsed into structure with TTP.

    TextFSM parses tabular `show` output; config is hierarchical, which is what
    TTP handles. See automation/ttp/templates/.
    """
    check_device_name(device)
    try:
        return tasks.get_config_structured(device, kind=kind)
    except tasks.DeviceError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ttp_parse.TTPParseError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"{device}: {exc}") from exc
