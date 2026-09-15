# The automation container: Nornir, Netmiko, TextFSM, TTP and the assurance
# engine in one image.
# It is the only thing in the stack that opens an SSH session to a device.
FROM python:3.12-slim
# Netmiko connects through paramiko, so no system ssh client is needed.

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY automation/service/requirements.txt .
# --timeout/--retries are kept: a slow or contended link should not fail a
# build, which is a bad first experience for someone installing on their own VM.
RUN pip install --no-cache-dir --timeout 120 --retries 5 -r requirements.txt

# The automation tree is bind-mounted at runtime (see compose/automation.yaml)
# so templates and tasks can be edited and retried without a rebuild. Copying
# it here too means the image still runs standalone.
COPY automation /app/automation
COPY platforms.yml /app/platforms.yml

# /app ONLY. Adding /app/automation would put directories named nornir,
# textfsm and ttp on the path, shadowing the real libraries.
ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    NET_TEXTFSM=/app/automation/textfsm

EXPOSE 8100
CMD ["uvicorn", "automation.service.main:app", "--host", "0.0.0.0", "--port", "8100"]
