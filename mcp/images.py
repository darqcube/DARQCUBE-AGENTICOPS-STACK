"""Short-lived images an AI platform shows in its chat, by link.

A rendered graph cannot travel inside a tool result to a chat: platforms
drop non-text tool content, and a PNG in a model's context costs thousands
of tokens per turn. So the tool keeps the image here and returns a LINK; the
model puts that link in its answer as Markdown, and the user's browser
fetches the image directly.

A browser <img> cannot send a bearer token, so the link is its own
authorisation: a readable label plus a random suffix the store forgets after
the TTL — `cr2-et0-1-traffic-1h-k7m2x9q4ab`. The label is for the MODEL: a
model copies words reliably but mangles long random strings (seen: three
characters dropped from the middle of a 22-character base64 id, so the image
never loaded). The suffix is the secret: 10 base32 characters, 50 bits,
alive 15 minutes among at most a few hundred images — not guessable in that
window. Bounded twice — by count and by total bytes — so a busy chat cannot
grow memory without limit.
"""
from __future__ import annotations

import base64
import re
import secrets
import threading
import time
from collections import OrderedDict

SUFFIX_CHARS = 10                      # base32: 5 bits each -> 50 bits
ID = re.compile(r"^(?:[a-z0-9]+-){0,8}[a-z2-7]{10}$")


def _slug(label: str) -> str:
    """`cr2 Et0/1 traffic 1h` -> `cr2-et0-1-traffic-1h` (at most 8 words)."""
    words = re.findall(r"[a-z0-9]+", label.lower())
    return "-".join(words[:8])


def _suffix() -> str:
    raw = base64.b32encode(secrets.token_bytes(7)).decode().lower()
    return raw[:SUFFIX_CHARS]


class ImageStore:
    def __init__(self, ttl_seconds: int = 900, max_items: int = 200, max_bytes: int = 64 * 1024 * 1024):
        self.ttl, self.max_items, self.max_bytes = ttl_seconds, max_items, max_bytes
        self._items: OrderedDict[str, tuple[float, bytes, str]] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def put(self, data: bytes, content_type: str = "image/png", label: str = "") -> str:
        slug = _slug(label)
        image_id = f"{slug}-{_suffix()}" if slug else _suffix()
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
