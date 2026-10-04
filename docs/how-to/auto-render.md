# Auto-render — collectors follow Infrahub without `make render`

By default a change in Infrahub reaches the collectors when someone runs
`make render`. Auto-render does that for you: Infrahub calls a small receiver
on every change to `main`, and the receiver re-runs the same renderer.

**Off by default.** Turn it on where Infrahub is edited often (the UI,
merged Proposed Changes) and nobody should have to remember `make render`.

```
Infrahub main ──signed webhook──▶ render-hook ──(15 s after the last event)──▶ render-inventory.py
                                                                            ──▶ Telegraf / Logstash reload
```

## Turn it on

1. Enable the profile — either in `site.yml` and re-run the installer:

   ```yaml
   source_of_truth:
     auto_render: true
   ```

   or add `auto-render` to `COMPOSE_PROFILES` in `.env`.

2. Make sure `.env` has a `RENDER_HOOK_SECRET`. New installs generate it; on an
   existing one add it once:

   ```sh
   echo "RENDER_HOOK_SECRET=$(python3 -c 'import secrets; print(secrets.token_hex(24))')" >> .env
   ```

3. Start it and register the webhook in Infrahub:

   ```sh
   make up
   make auto-render      # creates/updates the Infrahub webhook "darqcube-auto-render"
   ```

## What triggers a render

| Event | Renders |
|---|---|
| a `Network*` node or a tag created, updated or deleted on `main` | yes |
| a branch or Proposed Change merged into `main` | yes |
| a schema update | yes |
| anything else (validators, artifacts, threads, other kinds) | no — acknowledged and ignored |
| changes on any branch other than `main` | never sent (`branch_scope: default_branch`) |

A burst — `make seed` of 40 devices — renders **once**, `RENDER_HOOK_DEBOUNCE`
seconds (default 15) after the last event.

## Security

| Property | How |
|---|---|
| Only Infrahub can trigger it | every request is HMAC-signed with `RENDER_HOOK_SECRET` (Standard Webhooks: `webhook-id`, `webhook-timestamp`, `webhook-signature`); unsigned, mis-signed or tampered requests get 401 |
| No replay | requests older than five minutes are refused |
| Not reachable from outside | `expose:` only on the compose network — never a published port |
| Refuses to run open | without a secret the service exits rather than accepting unsigned calls |

## Check it

```sh
docker compose logs --since 10m render-hook
#  render-hook: accepted infrahub.node.updated NetworkDevice — render in 15s
#  render-hook: render ok in 1.2s
docker compose exec render-hook python -c \
  "import urllib.request; print(urllib.request.urlopen('http://localhost:8099/healthz').read().decode())"
#  {"last_render": "...", "last_ok": true, "pending": false, "renders": 3}
```

## Turn it off

```sh
make auto-render-off      # deactivates the webhook in Infrahub
```

then remove `auto-render` from `COMPOSE_PROFILES` (or set `auto_render: false`)
and `make up`. `make render` works the same either way.

## Problems

| Symptom | Cause |
|---|---|
| `refused: bad signature` in the log | `RENDER_HOOK_SECRET` changed after `make auto-render` — run it again |
| nothing in the log after a change | the webhook is not registered or inactive — `make auto-render`; or the change was on a branch, not `main` |
| `render FAILED` | the same failure `make render` would show — read the lines after it |
