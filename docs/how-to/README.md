# How to change things

Task-oriented guides. Each one is a single job, start to finish, with the exact
files and commands. If you are looking for *what a component is*, see
[../install/](../install/) instead.

## Devices and vendors

| I want to… | Guide |
|---|---|
| Add a device to the stack | [add-a-device.md](add-a-device.md) |
| Remove or decommission a device | [add-a-device.md](add-a-device.md#removing-a-device) |
| Support a new vendor or platform | [add-a-platform.md](add-a-platform.md) |
| Fix "no template" or empty parse errors (`show` output) | [add-a-textfsm-template.md](add-a-textfsm-template.md) |
| Parse a device **configuration** into structure | [add-a-ttp-template.md](add-a-ttp-template.md) |
| Add or change an assurance check | [change-assurance-rules.md](change-assurance-rules.md) |
| Parse a new vendor's syslog format | [add-a-syslog-format.md](add-a-syslog-format.md) |

## Monitoring

| I want to… | Guide |
|---|---|
| Add or change an alert | [change-alerts.md](change-alerts.md) |
| Add a metric from a new OID | [add-a-metric.md](add-a-metric.md) |
| Add a dashboard or a panel | [change-dashboards.md](change-dashboards.md) |
| Send alerts somewhere (AI platform, Slack, email) | [change-alerts.md](change-alerts.md#sending-alerts-somewhere) |

## The stack itself

| I want to… | Guide |
|---|---|
| Change a published port | [change-ports.md](change-ports.md) |
| Change retention, intervals or resource limits | [change-settings.md](change-settings.md) |
| Add a whole new service to the stack | [add-a-service.md](add-a-service.md) |
| Connect an AI platform | [connect-an-ai-platform.md](connect-an-ai-platform.md) |
| Deploy at a customer site (PoC or small production) | [deploy-on-customer-premises.md](deploy-on-customer-premises.md) |
| Work out why something is not working | [troubleshooting.md](troubleshooting.md) |
| Find data loss that produces **no error at all** | [fix-silent-data-loss.md](fix-silent-data-loss.md) |

## The two rules behind all of it

**1. Edit the source, never the output.** `observability/telegraf/generated/`
is written by `make render`. Anything you put there is gone at the next render,
with no warning.

**2. Two commands propagate a change.** `make seed` updates the source of
truth; `make render` pushes it into the collectors. A device added without
`make render` exists in Infrahub and is polled by nothing.
