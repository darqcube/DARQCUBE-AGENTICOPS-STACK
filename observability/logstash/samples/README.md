# Captured syslog samples

Real wire-format lines from each supported vendor, used as test fixtures by
`automation/tests/test_syslog_parsing.py`.

**Add a sample whenever you add a vendor or hit a line that parses wrongly.**
A grok pattern is a regex against one vendor's exact output format; without a
committed sample there is no way to tell whether a later edit broke it, because
an unmatched line does not raise — it just quietly falls through to the
catch-all and loses its labels.

Capture a real line with:

    docker compose exec logstash tcpdump -A -n -i any udp port 514
