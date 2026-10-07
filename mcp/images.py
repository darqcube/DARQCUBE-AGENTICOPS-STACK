"""Short-lived images an AI platform shows in its chat, by link.

A rendered graph cannot travel inside a tool result to a chat: platforms
drop non-text tool content, and a PNG in a model's context costs thousands
of tokens per turn. So the tool keeps the image here and returns a LINK; the
model puts that link in its answer as Markdown, and the user's browser
fetches the image directly.

A browser <img> cannot send a bearer token, so the link is its own
authorisation: a random 128-bit id (unguessable, like a signed URL, but short
enough for a small model to copy exactly) that the store forgets after the
TTL. Bounded twice — by count and by total bytes — so a busy chat cannot
grow memory without limit.
"""
from __future__ import annotations

import re
import secrets
import threading
import time
from collections import OrderedDict

ID = re.compile(r"^[A-Za-z0-9_-]{22}$")


class ImageStore:
    def __init__(self, ttl_seconds: int = 900, max_items: int = 200, max_bytes: int = 64 * 1024 * 1024):
        self.ttl, self.max_items, self.max_bytes = ttl_seconds, max_items, max_bytes
        self._items: OrderedDict[str, tuple[float, bytes, str]] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def put(self, data: bytes, content_type: str = "image/png") -> str:
        image_id = secrets.token_urlsafe(16)          # 22 chars, 128 bits
        with self._lock:
            self._expire()
            self._items[image_id] = (time.monotonic() + self.ttl, data, content_type)
            self._bytes += len(data)
            # Oldest first, until both bounds hold again.
            while len(self._items) > self.max_items or self._bytes > self.max_bytes:
                _, (_, old, _) = self._items.popitem(last=False)
                self._bytes -= len(old)
        return image_id

    def get(self, image_id: str) -> tuple[bytes, str] | None:
        if not ID.fullmatch(image_id or ""):
            return None
        with self._lock:
            self._expire()
            item = self._items.get(image_id)
        return (item[1], item[2]) if item else None

    def __len__(self) -> int:
        with self._lock:
            self._expire()
            return len(self._items)

    def _expire(self) -> None:
        now = time.monotonic()
        for key in [k for k, (exp, _, _) in self._items.items() if exp <= now]:
            _, data, _ = self._items.pop(key)
            self._bytes -= len(data)
