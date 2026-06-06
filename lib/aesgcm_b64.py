from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


NONCE_LEN = 12
TAG_LEN = 16


class IncrementalBase64Encoder:
    """Incrementally encode bytes to Base64 without newlines."""

    def __init__(self) -> None:
        self._carry = b""

    def feed(self, data: bytes) -> bytes:
        if not data:
            return b""
        buf = self._carry + data
        whole_len = (len(buf) // 3) * 3
        whole, self._carry = buf[:whole_len], buf[whole_len:]
        if not whole:
            return b""
        return base64.b64encode(whole)

    def finalize(self) -> bytes:
        if not self._carry:
            return b""
        out = base64.b64encode(self._carry)
        self._carry = b""
        return out


@dataclass(frozen=True)
class EncryptResult:
    nonce: bytes
    tag: bytes
    sha256_plaintext_hex: str


def encrypt_file_to_b64_aes256gcm(
    *,
    key: bytes,
    plaintext_path: Path,
    out_b64_path: Path,
    nonce: bytes | None = None,
) -> EncryptResult:
    """Encrypt a file with AES-256-GCM and write Base64(nonce||ciphertext||tag).

    - nonce: 12 bytes (random if not provided)
    - tag: 16 bytes (GCM)

    The output file is written via a temp file and atomically renamed.
    """
    if len(key) != 32:
        raise ValueError("key must be 32 bytes for AES-256")

    if nonce is None:
        nonce = os.urandom(NONCE_LEN)
    if len(nonce) != NONCE_LEN:
        raise ValueError("nonce must be 12 bytes")

    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()

    sha256 = hashlib.sha256()
    b64 = IncrementalBase64Encoder()

    tmp_path = out_b64_path.with_name(out_b64_path.name + ".tmp")
    with open(tmp_path, "wb") as out_f:
        out_f.write(b64.feed(nonce))

        with open(plaintext_path, "rb") as in_f:
            while True:
                chunk = in_f.read(1024 * 1024)
                if not chunk:
                    break
                sha256.update(chunk)
                ct = encryptor.update(chunk)
                if ct:
                    out_f.write(b64.feed(ct))

        final_ct = encryptor.finalize()
        if final_ct:
            out_f.write(b64.feed(final_ct))

        tag = encryptor.tag
        out_f.write(b64.feed(tag))
        out_f.write(b64.finalize())

        out_f.flush()
        os.fsync(out_f.fileno())

    os.replace(tmp_path, out_b64_path)

    return EncryptResult(nonce=nonce, tag=tag, sha256_plaintext_hex=sha256.hexdigest())
