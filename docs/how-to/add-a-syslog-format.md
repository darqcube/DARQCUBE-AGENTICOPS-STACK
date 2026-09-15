# Parse a new vendor's syslog format

Logstash owns syslog in this stack. Patterns live in
`observability/logstash/patterns/network.grok`, the pipeline in
`observability/logstash/pipeline/syslog.conf`.

## 1. Capture a real line

```bash
docker compose logs logstash | grep _grokparsefailure | head -3
```

Unparsed lines are **kept**, not dropped, precisely so you can find them. They
arrive in Loki with `device="unparsed"` and the `not_in_source_of_truth` tag.

## 2. Write the pattern

```
# --- Arista EOS ---------------------------------------------------------
# <190>Sep 14 10:23:45 sw1 Ebra: %LINEPROTO-5-UPDOWN: Interface Et1, changed
EOS_SYSLOG <%{NONNEGINT:pri:int}>%{SYSLOGTIMESTAMP:ts} %{HOSTNAME:hostname} %{WORD:agent}: %%{DATA:facility}-%{INT:severity_code:int}-%{WORD:mnemonic}: %{GREEDYDATA:msg}
```

Required captures: `pri`, `hostname`, `msg`. Optional but useful: `facility`,
`severity_code`, `mnemonic`.

### Word boundaries — the trap that cost an hour here

Grok's `NONNEGINT` is `\b[0-9]+\b` and `WORD` is `\b\w+\b`. Both need a word
boundary. In Huawei's `%%01IFNET` there is **no boundary** between the `1` and
the `I` — both are word characters — so neither pattern can match and the whole
line falls through to the catch-all with no error anywhere.

Wherever a vendor runs tokens together without a separator, use explicit
classes:

```
%%(?<vrp_ver>[0-9]+)(?<facility>[A-Za-z0-9]+)/...
```

### Escaping `%`

In a grok pattern file a `%` is literal unless followed by `{`. To match the two
percent signs Huawei sends, write exactly `%%`.

## 3. Register it

In `observability/logstash/pipeline/syslog.conf`, before the catch-all:

```ruby
      "message" => [
        "%{CISCO_SYSLOG}",
        "%{VRP_SYSLOG}",
        "%{ROS_SYSLOG}",
        "%{EOS_SYSLOG}",
        "%{ANY_SYSLOG}"      # keep last — it matches anything
      ]
```

Order matters: first match wins.

## 4. Add a sample and test

`observability/logstash/samples/syslog-samples.txt` — one real line per vendor.

```bash
.venv/bin/python -m pytest automation/tests/test_syslog_parsing.py -v
```

That test runs the **real** filter block against the samples, so it cannot pass
against a pipeline the stack does not use.

## 5. Apply

```bash
docker compose restart logstash
```

## Label discipline

Only these become Loki labels:

```ruby
include_fields => ["device", "site", "role", "severity", "platform"]
```

Do not add `mnemonic`, `topics`, or anything per-message. Loki creates a stream
per unique label combination, so promoting a mnemonic multiplies stream count by
the number of distinct message types a device can emit. It is cheap to prevent
and expensive to undo. A test enforces this list.

The message body and mnemonics stay searchable as content:

```logql
{device="cr1"} |= "LINK_STATE"
```

## Severity

Cisco and Huawei carry severity in the message body (`%SSH-5-...`). RouterOS
does not, so the pipeline derives it from the PRI (`pri % 8`). If your vendor
has no body severity, capture `pri` and the existing fallback handles it.
