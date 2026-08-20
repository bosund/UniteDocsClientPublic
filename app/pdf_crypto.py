"""Standalone PDF standard-security-handler password verification.

Motivation
----------
The historical password guesser called ``pdfium.PdfDocument(data, password=...)``
(or the C accelerator's ``FPDF_LoadMemDocument``) once per candidate: it
constructs a document object and runs the encryption's key-derivation on every
attempt.

Testing a password does not require opening the document at all. The
``/Encrypt`` dictionary and the trailer ``/ID`` are stored unencrypted, and the
standard security handler verifies a password with a small, self-contained
hash/key-derivation against those values. This module extracts those parameters
once and then verifies each candidate with only the key-derivation step. This is
the same idea behind ``pdf2john`` and hashcat's PDF modes.

Measured single-process throughput (4-core dev box, wrong candidates):

* Revision 2-4 (RC4 / AES-128): roughly on par with pdfium -- pdfium checks the
  password lazily without a full parse, and the cost is dominated by the shared
  MD5/RC4 key derivation. No regression, but little to gain here.
* Revision 6 (AES-256): about 3-4x faster per attempt, and this is exactly the
  case the old code struggled with -- the AES-256 hardened hash made each
  ``PdfDocument`` call slow enough to freeze the UI, which is why the guesser
  grew its subprocess / C-accelerator machinery. Verifying the hash directly is
  faster, parallelises cleanly, and needs no C extension or DLL discovery.

Supported: standard security handler revisions 2, 3, 4 (RC4 / AES-128) and 6
(AES-256, PDF 2.0), plus the deprecated revision 5. Public-key handlers and
unknown revisions return ``None`` from :func:`extract_encryption_params`, and the
caller is expected to fall back to opening the document.

Only the *primitives* not in the standard library are taken from an optional
crypto backend:

* RC4  -> pycryptodome ``ARC4`` if present, otherwise a small pure-Python RC4.
* AES-128-CBC (revision 6 only) -> pycryptodome ``AES`` if present, otherwise
  the ``cryptography`` package.

pycryptodome is BSD-licensed (commercially compatible). Revisions 2-4 need no
third-party crypto at all; only revision 6 requires an AES implementation.
"""

from __future__ import annotations

import hashlib
import re
import struct
from dataclasses import dataclass

# Algorithm 2 padding string (ISO 32000-1:2008, 7.6.3.3).
_PAD = bytes([
    0x28, 0xBF, 0x4E, 0x5E, 0x4E, 0x75, 0x8A, 0x41, 0x64, 0x00, 0x4E, 0x56,
    0xFF, 0xFA, 0x01, 0x08, 0x2E, 0x2E, 0x00, 0xB6, 0xD0, 0x68, 0x3E, 0x80,
    0x2F, 0x0C, 0xA9, 0xFE, 0x64, 0x53, 0x69, 0x7A,
])


# --------------------------------------------------------------------------- #
# Crypto primitives (optional accelerated backends, pure-Python fallbacks)
# --------------------------------------------------------------------------- #

try:
    from Crypto.Cipher import ARC4 as _ARC4  # pycryptodome

    def _rc4(key: bytes, data: bytes) -> bytes:
        return _ARC4.new(key).encrypt(data)
except ImportError:  # pragma: no cover - fallback path
    def _rc4(key: bytes, data: bytes) -> bytes:
        S = list(range(256))
        j = 0
        klen = len(key)
        for i in range(256):
            j = (j + S[i] + key[i % klen]) & 0xFF
            S[i], S[j] = S[j], S[i]
        out = bytearray(len(data))
        i = j = 0
        for n, b in enumerate(data):
            i = (i + 1) & 0xFF
            j = (j + S[i]) & 0xFF
            S[i], S[j] = S[j], S[i]
            out[n] = b ^ S[(S[i] + S[j]) & 0xFF]
        return bytes(out)


# Resolve the AES-CBC backend once at import time. The revision-6 hardened hash
# (Algorithm 2.B) runs ~64 AES-CBC encryptions per password attempt, so a
# per-call ``import`` is catastrophic: when pycryptodome is absent the failing
# ``from Crypto.Cipher import AES`` re-runs Python's full import machinery
# (filesystem stat walk) on every round, which measured as ~92% of total
# verification time. Binding the backend here mirrors the RC4 pattern above.
try:
    from Crypto.Cipher import AES as _AES  # pycryptodome

    def _aes_cbc_encrypt_nopad(key: bytes, iv: bytes, data: bytes) -> bytes:
        return _AES.new(key, _AES.MODE_CBC, iv).encrypt(data)
except ImportError:  # pragma: no cover - fallback path
    from cryptography.hazmat.primitives.ciphers import (
        Cipher as _Cipher,
        algorithms as _algorithms,
        modes as _modes,
    )

    def _aes_cbc_encrypt_nopad(key: bytes, iv: bytes, data: bytes) -> bytes:
        enc = _Cipher(_algorithms.AES(key), _modes.CBC(iv)).encryptor()
        return enc.update(data) + enc.finalize()


# --------------------------------------------------------------------------- #
# PDF token parsing (raw bytes, no document load)
# --------------------------------------------------------------------------- #

def _parse_pdf_string(data: bytes, pos: int):
    """Parse a PDF hex string ``<...>`` or literal string ``(...)`` at ``pos``
    (leading whitespace allowed). Returns ``(value_bytes, end_pos)``."""
    while pos < len(data) and data[pos:pos + 1].isspace():
        pos += 1
    ch = data[pos:pos + 1]
    if ch == b'<':
        end = data.index(b'>', pos)
        hexs = re.sub(rb'\s', b'', data[pos + 1:end])
        if len(hexs) % 2:
            hexs += b'0'
        return bytes.fromhex(hexs.decode('ascii')), end + 1
    if ch == b'(':
        out = bytearray()
        i = pos + 1
        depth = 1
        simple = {0x6E: 0x0A, 0x72: 0x0D, 0x74: 0x09, 0x62: 0x08,
                  0x66: 0x0C, 0x28: 0x28, 0x29: 0x29, 0x5C: 0x5C}
        while i < len(data):
            c = data[i]
            if c == 0x5C:  # backslash escape
                nxt = data[i + 1]
                if nxt in simple:
                    out.append(simple[nxt]); i += 2; continue
                if 0x30 <= nxt <= 0x37:  # up to 3 octal digits
                    j = i + 1
                    digits = b''
                    while j < len(data) and len(digits) < 3 and 0x30 <= data[j] <= 0x37:
                        digits += data[j:j + 1]; j += 1
                    out.append(int(digits, 8) & 0xFF); i = j; continue
                if nxt in (0x0A, 0x0D):  # escaped line continuation
                    i += 2; continue
                out.append(nxt); i += 2; continue
            if c == 0x28:
                depth += 1; out.append(c); i += 1; continue
            if c == 0x29:
                depth -= 1
                if depth == 0:
                    return bytes(out), i + 1
                out.append(c); i += 1; continue
            out.append(c); i += 1
        return bytes(out), i
    raise ValueError("expected a PDF string")


def _extract_dict_bytes(data: bytes, start: int) -> bytes:
    """Return the bytes of the dictionary whose opening ``<<`` is at ``start``,
    balancing nested ``<<``/``>>`` and skipping string literals."""
    depth = 0
    i = start
    n = len(data)
    while i < n - 1:
        pair = data[i:i + 2]
        if pair == b'<<':
            depth += 1; i += 2; continue
        if pair == b'>>':
            depth -= 1; i += 2
            if depth == 0:
                return data[start:i]
            continue
        if data[i:i + 1] == b'(':
            _, i = _parse_pdf_string(data, i); continue
        i += 1
    return data[start:]


def _find_string_value(obj: bytes, key: bytes) -> bytes:
    m = re.search(re.escape(key) + rb'\s*(?=[<(])', obj)
    if not m:
        return b''
    try:
        val, _ = _parse_pdf_string(obj, m.end())
        return val
    except Exception:
        return b''


# --------------------------------------------------------------------------- #
# Extracted parameters
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class EncParams:
    """Password-independent parameters of the standard security handler.

    A frozen dataclass so it pickles cleanly across multiprocessing workers.
    """
    V: int
    R: int
    length: int          # key length in bits
    P: int               # permissions (signed 32-bit)
    O: bytes
    U: bytes
    OE: bytes
    UE: bytes
    id0: bytes           # first element of trailer /ID
    encrypt_metadata: bool

    @property
    def supported(self) -> bool:
        return self.R in (2, 3, 4, 5, 6)


def extract_encryption_params(data: bytes) -> EncParams | None:
    """Extract the standard-security-handler parameters from raw PDF bytes.

    Returns ``None`` when the file is not encrypted, uses a non-standard
    security handler, or the parameters cannot be located -- callers should
    fall back to opening the document in that case.
    """
    # /Encrypt is either an indirect reference (N G R) or an inline dictionary.
    obj = None
    m = re.search(rb'/Encrypt\s+(\d+)\s+(\d+)\s+R', data)
    if m:
        mo = re.search(m.group(1) + rb'\s+\d+\s+obj', data)
        if mo:
            end = data.find(b'endobj', mo.end())
            obj = data[mo.end():end if end != -1 else len(data)]
    if obj is None:
        mi = re.search(rb'/Encrypt\s*<<', data)
        if not mi:
            return None
        obj = _extract_dict_bytes(data, data.index(b'<<', mi.start()))

    # Only the standard security handler is supported here.
    mf = re.search(rb'/Filter\s*/([A-Za-z0-9.]+)', obj)
    if mf and mf.group(1) != b'Standard':
        return None

    try:
        R = int(re.search(rb'/R\s+(-?\d+)', obj).group(1))
        V = int(re.search(rb'/V\s+(-?\d+)', obj).group(1))
        P = int(re.search(rb'/P\s+(-?\d+)', obj).group(1))
    except (AttributeError, ValueError):
        return None

    ml = re.search(rb'/Length\s+(\d+)', obj)
    length = int(ml.group(1)) if ml else 40
    mem = re.search(rb'/EncryptMetadata\s+(true|false)', obj)
    encrypt_metadata = (mem is None) or (mem.group(1) == b'true')

    id0 = b''
    mid = re.search(rb'/ID\s*\[', data)
    if mid:
        try:
            id0, _ = _parse_pdf_string(data, mid.end())
        except Exception:
            id0 = b''

    params = EncParams(
        V=V, R=R, length=length, P=P,
        O=_find_string_value(obj, b'/O'),
        U=_find_string_value(obj, b'/U'),
        OE=_find_string_value(obj, b'/OE'),
        UE=_find_string_value(obj, b'/UE'),
        id0=id0,
        encrypt_metadata=encrypt_metadata,
    )
    if not params.supported or not params.U:
        return None
    return params


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #

def _hash_2b(pw: bytes, salt: bytes, udata: bytes = b'') -> bytes:
    """ISO 32000-2 Algorithm 2.B hardened hash used by revision 6."""
    K = hashlib.sha256(pw + salt + udata).digest()
    rnd = 0
    while True:
        K1 = (pw + K + udata) * 64
        E = _aes_cbc_encrypt_nopad(K[:16], K[16:32], K1)
        # First 16 bytes of E as a big-endian integer mod 3; because
        # 256 == 1 (mod 3), that equals the byte-sum mod 3.
        mod = sum(E[:16]) % 3
        K = (hashlib.sha256 if mod == 0
             else hashlib.sha384 if mod == 1
             else hashlib.sha512)(E).digest()
        rnd += 1
        if rnd >= 64 and E[-1] <= rnd - 32:
            break
    return K[:32]


def make_verifier(params: EncParams):
    """Build a fast ``verify(candidate: str) -> bool`` closure for the user
    password, precomputing everything that does not depend on the candidate."""
    R = params.R

    if R >= 5:
        validation_salt = params.U[32:40]
        expected = params.U[:32]
        if R == 5:
            # Deprecated revision 5: plain SHA-256, no Algorithm 2.B rounds.
            def verify(candidate: str) -> bool:
                pw = candidate.encode('utf-8')[:127]
                return hashlib.sha256(pw + validation_salt).digest() == expected
        else:
            def verify(candidate: str) -> bool:
                pw = candidate.encode('utf-8')[:127]
                return _hash_2b(pw, validation_salt) == expected
        return verify

    # Revisions 2-4.
    n = params.length // 8
    tail = params.O[:32] + struct.pack('<i', params.P) + params.id0
    if R >= 4 and not params.encrypt_metadata:
        tail += b'\xff\xff\xff\xff'
    md5 = hashlib.md5

    if R == 2:
        expected = params.U[:32]

        def verify(candidate: str) -> bool:
            pw = candidate.encode('latin-1', 'replace')
            key = md5((pw + _PAD)[:32] + tail).digest()[:n]
            return _rc4(key, _PAD)[:32] == expected
        return verify

    # R3 / R4: Algorithm 2 with 50 extra MD5 rounds, Algorithm 5 for /U.
    x = md5(_PAD + params.id0).digest()  # candidate-independent
    expected = params.U[:16]

    def verify(candidate: str) -> bool:
        pw = candidate.encode('latin-1', 'replace')
        h = md5((pw + _PAD)[:32] + tail).digest()
        for _ in range(50):
            h = md5(h[:n]).digest()
        key = h[:n]
        val = _rc4(key, x)
        for i in range(1, 20):
            val = _rc4(bytes(b ^ i for b in key), val)
        return val[:16] == expected
    return verify


def verify_user_password(params: EncParams, candidate: str) -> bool:
    """Convenience one-shot verification (builds a verifier each call)."""
    return make_verifier(params)(candidate)
