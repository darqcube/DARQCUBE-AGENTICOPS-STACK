# Pin SSH host keys in Infrahub

Every SSH session the stack opens — Netmiko for `show` commands and config
pushes, pyATS for assurance and the pyATS tools — should prove it reached the
real device. That proof is the device's SSH **host key**. This guide makes
Infrahub, the source of truth for *which* devices exist, also the source of
truth for *how each one proves who it is*.

| | Without pinned keys | With keys pinned in Infrahub |
|---|---|---|
| pyATS | learns a device's key on first connect, remembered by device name; a later change is refused | accepts only the keys in Infrahub |
| Netmiko | accepts any key | accepts only the keys in Infrahub |
| A device's address changes | no effect (keys are filed by name) | no effect |
| A device is re-keyed or replaced | pyATS refuses until the learned key is removed | every session refuses until the new key is pinned |
| Rebuilding the automation host | keys are re-learned | nothing to re-learn: Infrahub holds them |

## Pin every device

```bash
make pin-host-keys
```

For each device in Infrahub this scans the keys the device presents now and:

| Result | Meaning |
|---|---|
| `pin` *verified against the learned key* | matches what pyATS learned on first connect; written to Infrahub |
| `pin` *first sight — verify the fingerprint* | never seen before; written to Infrahub. Compare the printed `SHA256:` fingerprint with the device's console |
| `unchanged` | Infrahub already holds these keys |
| `refused` | the key differs from the one in Infrahub or the one learned. **Nothing is written.** Find out why first |
| `failed` | no key answered: the device is unreachable or SSH is closed |

One device: `make pin-host-keys DEV=router2`.

The keys are stored on the device's node as `ssh_host_keys`, one
`<type> <base64>` per line — public keys, so nothing secret. They take effect
on the next connection (within the inventory cache TTL, 60 s by default).

## Check a fingerprint on the device

| Platform | Command |
|---|---|
| Cisco IOS-XE | `show ip ssh` (key type), `show crypto key mypubkey rsa` |
| Huawei VRP | `display rsa local-key-pair public` |
| MikroTik RouterOS | `/ip ssh print` |

On a host with OpenSSH, `ssh-keygen -lf <(ssh-keyscan <address>)` prints the
same `SHA256:` form the stack prints.

## A device was re-keyed or replaced

Sessions to it now refuse, which is the point. Once you have confirmed the new
fingerprint on the console:

```bash
make pin-host-keys DEV=router2 REPLACE=1
```

## Provision keys from the device YAML (strongest)

Keys taken from the device's console or its provisioning system are verified
out of band, not on first sight. Put them in the device record and seed:

```yaml
devices:
  - name: router2
    # …
    ssh_host_keys: |
      ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA…
```

`make seed` writes them. A record that leaves `ssh_host_keys` out keeps
whatever Infrahub holds: host keys are observed state, owned by
`make pin-host-keys`, so a seed never un-pins the fleet.

## If the value in Infrahub is malformed

Every session to that device is refused (it fails closed — a broken pin never
falls back to accepting any key), and the error names the bad line. Fix the
value in Infrahub or re-run `make pin-host-keys DEV=… REPLACE=1`.

## After upgrading

The attribute is new in the schema. Load it once:

```bash
make schema
```
