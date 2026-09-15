# One image, six servers. MCP_SERVER picks the module, PORT picks the port.
#
# Six containers from one build: the servers differ only in which backend they
# call and which tools they register, so separate images would be six copies of
# the same dependency set.
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common.py serve.py ./
COPY servers ./servers

ENV PYTHONUNBUFFERED=1
CMD ["python", "serve.py"]
