# Logstash with the Loki output plugin.
#
# A custom image is unavoidable: logstash-output-loki is not bundled, and it is
# the whole reason Logstash is in this stack (syslog -> parse -> enrich -> Loki).
FROM docker.elastic.co/logstash/logstash:8.15.3

# Remove the stock pipeline so ours is unambiguous.
RUN rm -f /usr/share/logstash/pipeline/logstash.conf

RUN logstash-plugin install logstash-output-loki

# Config is bind-mounted at runtime so a pipeline edit needs a restart, not a
# rebuild — see compose/observability.yaml.
