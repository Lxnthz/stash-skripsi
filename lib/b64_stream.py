from __future__ import annotations

import base64


class IncrementalBase64Decoder:
    """Incrementally decode Base64 (no newlines required).

    Keeps internal carry so callers can feed arbitrary chunk sizes.
    """

    def __init__(self) -> None:
        self._carry = b""

    def feed(self, data: bytes) -> bytes:
        if not data:
            return b""
        buf = self._carry + data
        # Base64 works in 4-byte quanta.
        whole_len = (len(buf) // 4) * 4
        whole, self._carry = buf[:whole_len], buf[whole_len:]
        if not whole:
            return b""
        # validate=False for speed; our producer is controlled
        return base64.b64decode(whole, validate=False)

    def finalize(self) -> bytes:
        if not self._carry:
            return b""
        out = base64.b64decode(self._carry, validate=False)
        self._carry = b""
        return out
