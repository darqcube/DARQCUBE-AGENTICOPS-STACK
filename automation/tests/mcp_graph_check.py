"""Runs INSIDE darqcube/mcp:local, driven by test_mcp_graphs.py.

The real mcp-grafana module with graphs enabled. Grafana's render endpoint and
Prometheus are replaced; everything else — validation, the image store, the
public image route behind the real auth middleware — is the shipped code.

Prints one JSON object: {check name: [passed, detail]}.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import traceback

os.environ.update({
    "MCP_GRAPHS_ENABLED": "true",
    "MCP_IMAGE_BASE_URL": "http://192.0.2.10:9004",
    "GRAFANA_PUBLIC_URL": "http://192.0.2.10:13000",
    "MCP_AUTH_TOKEN": "test-token",
    "GRAFANA_PASSWORD": "x",
})
sys.path.insert(0, "/app")

from starlette.testclient import TestClient  # noqa: E402

import common  # noqa: E402
from images import ImageStore  # noqa: E402
from servers import grafana  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
renders: list[dict] = []
results: dict[str, list] = {}


def fake_get_bytes(path, params=None, *, max_bytes, content_type):
    renders.append({"path": path, "params": dict(params or {}), "max_bytes": max_bytes,
                    "content_type": content_type})
    return PNG


queries: list[str] = []


def fake_prometheus_get(path, params=None):
    q = (params or {}).get("query", "")
    queries.append(q)
    value = "5e6" if "max_over_time" in q else ("2e6" if "avg_over_time" in q else "1e6")
    return {"status": "success", "data": {"result": [{"metric": {}, "value": [0, value]}]}}


grafana.render_api.get_bytes = fake_get_bytes
grafana.prometheus.get = fake_prometheus_get


def check(name, fn):
    try:
        ok, detail = fn()
    except Exception:
        ok, detail = False, traceback.format_exc()[-600:]
    results[name] = [bool(ok), str(detail)[:600]]


def call(**kwargs):
    # A registered sync tool is wrapped to run in a thread: await it.
    return asyncio.run(grafana.render_interface_graph(**kwargs))


def raises(**kwargs):
    try:
        call(**kwargs)
    except common.BoundsError:
        return True
    return False


tools = {t.name for t in grafana.mcp._tool_manager.list_tools()}
check("render_tool_registered_when_enabled", lambda: ("render_interface_graph" in tools, sorted(tools)))

out = call(device="cr2", interface="Et0/1", window="1h", kind="traffic")
r = renders[-1] if renders else {}
check("returns_markdown_image_on_the_public_base",
      lambda: (out["markdown"].startswith("![cr2 Et0/1 traffic, last 1h](http://192.0.2.10:9004/g/")
               and out["markdown"].endswith(".png)") and out["image_url"] in out["markdown"], out))
name = out["image_url"].rsplit("/", 1)[1]
check("image_link_is_readable_with_a_random_suffix",
      lambda: (name.startswith("cr2-et0-1-traffic-1h-") and name.endswith(".png")
               and len(name.removesuffix(".png").rsplit("-", 1)[1]) == 10, name))
other = call(device="cr2", interface="Et0/1", window="1h", kind="traffic")
check("same_request_gets_a_different_link",
      lambda: (other["image_url"] != out["image_url"], (out["image_url"], other["image_url"])))
check("renders_only_the_fixed_dashboard_panel",
      lambda: (r.get("path") == "/render/d-solo/darqcube-interface/interface-detail"
               and r["params"]["panelId"] == 1, r))
check("render_request_carries_validated_vars_and_window",
      lambda: (r["params"]["var-device"] == "cr2" and r["params"]["var-interface"] == "Et0/1"
               and r["params"]["from"] == "now-1h" and r["params"]["theme"] == "dark", r["params"]))
check("render_expects_png_and_caps_size",
      lambda: (r["content_type"] == "image/png" and r["max_bytes"] <= 4 * 1024 * 1024, r))
check("summary_has_the_numbers_in_readable_units",
      lambda: (out["summary"]["in"] == {"now": "1.00 Mbps", "avg": "2.00 Mbps", "max": "5.00 Mbps"}, out["summary"]))
check("summary_aggregates_across_series",
      lambda: (queries and all("sum(rate(" in q for q in queries), queries[:3]))
check("dashboard_link_scopes_device_and_interface",
      lambda: (out["dashboard_url"] == "http://192.0.2.10:13000/d/darqcube-interface?var-device=cr2&var-interface=Et0%2F1",
               out["dashboard_url"]))

errs = call(device="cr2", interface="Et0/1", kind="errors")
check("errors_kind_uses_the_errors_panel",
      lambda: (renders[-1]["params"]["panelId"] == 2 and "summary" in errs, renders[-1]))

check("bad_interface_refused", lambda: (raises(device="cr2", interface='Et0/1"} or vector(1)'), ""))
check("bad_device_refused", lambda: (raises(device="cr2;rm", interface="Et0/1"), ""))
check("window_over_24h_refused", lambda: (raises(device="cr2", interface="Et0/1", window="48h"), ""))
check("unknown_kind_refused", lambda: (raises(device="cr2", interface="Et0/1", kind="cpu"), ""))

# --- the public image route, behind the REAL auth middleware -----------------
app = grafana.mcp.streamable_http_app()
app.router.routes.extend(grafana.PUBLIC_ROUTES)
prefixes = tuple(rt.path.split("{", 1)[0] for rt in grafana.PUBLIC_ROUTES)
verifier = common.Verifier(mode="token", token="test-token")
with TestClient(common._Auth(app, verifier, "grafana", prefixes)) as client:
    path = out["image_url"].replace("http://192.0.2.10:9004", "")
    got = client.get(path)
    check("image_served_without_a_bearer_token",
          lambda: (got.status_code == 200 and got.content == PNG
                   and got.headers["content-type"] == "image/png", got.status_code))
    check("image_response_is_locked_down",
          lambda: (got.headers.get("x-content-type-options") == "nosniff"
                   and got.headers.get("content-security-policy") == "default-src 'none'"
                   and got.headers.get("cache-control", "").startswith("private"), dict(got.headers)))
    check("unknown_image_404",
          lambda: (client.get("/g/cr2-et0-1-traffic-1h-abcdefghij.png").status_code == 404, ""))
    check("malformed_image_name_404",
          lambda: (client.get("/g/..%2F..%2Fetc%2Fpasswd").status_code == 404
                   and client.get("/g/abc.png").status_code == 404, ""))
    check("exemption_is_only_the_image_route",
          lambda: (client.post("/mcp", json={}).status_code == 401, "MCP still needs a bearer token"))

# --- the store's bounds --------------------------------------------------------
short = ImageStore(ttl_seconds=1)
sid = short.put(PNG)
check("store_returns_what_it_kept", lambda: (short.get(sid) == (PNG, "image/png"), ""))
time.sleep(1.2)
check("store_forgets_after_ttl", lambda: (short.get(sid) is None and len(short) == 0, ""))
small = ImageStore(max_items=2)
ids = [small.put(PNG) for _ in range(3)]
check("store_evicts_oldest_by_count",
      lambda: (small.get(ids[0]) is None and small.get(ids[2]) is not None, len(small)))
tiny = ImageStore(max_bytes=len(PNG) * 2)
tids = [tiny.put(PNG) for _ in range(3)]
check("store_evicts_oldest_by_bytes", lambda: (tiny.get(tids[0]) is None and len(tiny) == 2, len(tiny)))

print(json.dumps(results))
