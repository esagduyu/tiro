"""Sync crypto (S3): passphrase -> Argon2id -> X25519 age identity.

Scheme (spec para 5, FROZEN): passphrase + per-library salt -> Argon2id
(params recorded in format.json) -> 32 raw bytes -> Bech32
"AGE-SECRET-KEY-1..." -> age X25519 identity via pyrage. The Bech32 string
IS the recovery code (shown once at `tiro sync setup`, S5). Deliberately
NOT age's native scrypt-passphrase mode: deriving the identity keeps the
roadmap's Argon2id and is reproducible in Swift (iOS v2) and browser (7b —
typage + argon2 wasm). Cross-port parity is frozen by
tests/fixtures/sync-crypto-parity.json — do NOT change derivation, Bech32
output, or format.json field names without a decision record + SYNC_FORMAT
review.

format.json (spec para 5, plaintext at the backend root):
    {"sync_format": 1, "library_id": ulid, "encryption": "age"|"none",
     "kdf": {"algo": "argon2id", "salt": b64, "m": KiB, "t": int, "p": int} | null,
     "age_recipient": "age1..." | null, "created_at": iso}

PURE module: no config, no SQLite, no network (zero-I/O gate:
tests/test_sync_quarantine.py). Argon2id defaults are the RFC 9106
low-memory profile; derivation always reads params from format.json,
never these constants.
"""

from __future__ import annotations

import base64
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

import pyrage
from argon2.low_level import Type, hash_secret_raw
from pyrage import x25519

from tiro.sync.journal import SYNC_FORMAT, canonical_json

ARGON2ID_M_KIB = 65536  # 64 MiB
ARGON2ID_T = 3
ARGON2ID_P = 4
KEY_LEN = 32
SALT_LEN = 16


class CryptoError(ValueError):
    """Key derivation, identity, or decryption failure. Quarantine-class
    (spec para 6.7): callers stop the cycle, never half-apply."""


class SyncFormatError(ValueError):
    """format.json unreadable, invalid, or a NEWER sync_format than this
    build understands (refuse + upgrade prompt, roadmap rule)."""


# --- Bech32 (BIP-173 reference implementation, MIT, by Pieter Wuille) ------
# Vendored (~40 lines) because age secret keys are Bech32 and pyrage has no
# raw-scalar Identity constructor (decision #2). Encoder only.

_B32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _b32_polymod(values: list[int]) -> int:
    gen = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for value in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ value
        for i in range(5):
            chk ^= gen[i] if ((top >> i) & 1) else 0
    return chk


def _b32_hrp_expand(hrp: str) -> list[int]:
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _b32_checksum(hrp: str, data: list[int]) -> list[int]:
    values = _b32_hrp_expand(hrp) + data
    polymod = _b32_polymod(values + [0, 0, 0, 0, 0, 0]) ^ 1
    return [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]


def _convertbits(data: bytes, frombits: int, tobits: int) -> list[int]:
    acc = 0
    bits = 0
    ret: list[int] = []
    maxv = (1 << tobits) - 1
    for value in data:
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if bits:
        ret.append((acc << (tobits - bits)) & maxv)
    return ret


def _bech32_encode(hrp: str, payload: bytes) -> str:
    data = _convertbits(payload, 8, 5)
    combined = data + _b32_checksum(hrp, data)
    return hrp + "1" + "".join(_B32_CHARSET[d] for d in combined)


# --- KDF --------------------------------------------------------------------


@dataclass(frozen=True)
class KdfParams:
    salt_b64: str
    m: int = ARGON2ID_M_KIB
    t: int = ARGON2ID_T
    p: int = ARGON2ID_P
    algo: str = "argon2id"

    def to_dict(self) -> dict:
        return {"algo": self.algo, "salt": self.salt_b64,
                "m": self.m, "t": self.t, "p": self.p}

    @classmethod
    def from_dict(cls, d: dict) -> KdfParams:
        try:
            if d["algo"] != "argon2id":
                raise CryptoError(f"unsupported kdf algo: {d['algo']!r}")
            return cls(salt_b64=d["salt"], m=int(d["m"]), t=int(d["t"]), p=int(d["p"]))
        except CryptoError:
            raise
        except Exception as e:
            raise CryptoError(f"malformed kdf params: {e}") from e


def new_kdf_params(*, salt: bytes | None = None) -> KdfParams:
    salt = salt if salt is not None else secrets.token_bytes(SALT_LEN)
    return KdfParams(salt_b64=base64.b64encode(salt).decode("ascii"))


def derive_secret_key(passphrase: str, kdf: KdfParams) -> bytes:
    if kdf.algo != "argon2id":
        raise CryptoError(f"unsupported kdf algo: {kdf.algo!r}")
    try:
        salt = base64.b64decode(kdf.salt_b64, validate=True)
    except Exception as e:
        raise CryptoError(f"bad kdf salt: {e}") from e
    try:
        return hash_secret_raw(
            secret=passphrase.encode("utf-8"),  # as-given; ports must NOT NFC-normalize
            salt=salt,
            time_cost=kdf.t,
            memory_cost=kdf.m,
            parallelism=kdf.p,
            hash_len=KEY_LEN,
            type=Type.ID,
        )
    except Exception as e:
        raise CryptoError(f"argon2id derivation failed: {e}") from e


def secret_key_to_recovery_code(key: bytes) -> str:
    """32 raw bytes -> 'AGE-SECRET-KEY-1...' (uppercase, age's own format).
    This string IS the recovery code (spec para 5, FROZEN)."""
    if len(key) != KEY_LEN:
        raise CryptoError(f"secret key must be {KEY_LEN} bytes, got {len(key)}")
    return _bech32_encode("age-secret-key-", key).upper()


def derive_recovery_code(passphrase: str, kdf: KdfParams) -> str:
    return secret_key_to_recovery_code(derive_secret_key(passphrase, kdf))


# --- Codecs -----------------------------------------------------------------


class PlainCodec:
    """Encryption OFF (filesystem-adapter default, spec para 5): blobs are
    written plaintext; format.json records encryption='none'."""

    encryption = "none"

    def encrypt(self, data: bytes) -> bytes:
        return data

    def decrypt(self, blob: bytes) -> bytes:
        return blob


class AgeCodec:
    """All-.age-blobs codec bound to one library identity (spec para 5)."""

    encryption = "age"

    def __init__(self, recovery_code: str):
        try:
            self._identity = x25519.Identity.from_str(recovery_code.strip())
        except Exception as e:
            raise CryptoError("invalid recovery code") from e
        self._recipient = self._identity.to_public()

    @property
    def recipient(self) -> str:
        return str(self._recipient)

    def encrypt(self, data: bytes) -> bytes:
        try:
            return pyrage.encrypt(data, [self._recipient])
        except Exception as e:  # pragma: no cover — encrypt failures are exotic
            raise CryptoError(f"age encryption failed: {e}") from e

    def decrypt(self, blob: bytes) -> bytes:
        try:
            return pyrage.decrypt(blob, [self._identity])
        except Exception as e:
            raise CryptoError(f"age decryption failed: {e}") from e


def codec_from_passphrase(passphrase: str, kdf: KdfParams) -> AgeCodec:
    return AgeCodec(derive_recovery_code(passphrase, kdf))


# --- format.json (spec para 5, plaintext at the backend root) ---------------


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class SyncFormat:
    sync_format: int
    library_id: str
    encryption: str  # "age" | "none"
    kdf: KdfParams | None
    age_recipient: str | None
    created_at: str


def build_format_json(
    library_id: str,
    *,
    kdf: KdfParams | None = None,
    age_recipient: str | None = None,
    now: str | None = None,
) -> str:
    """Canonical-JSON format.json. age mode needs BOTH kdf and recipient;
    plaintext mode (filesystem-adapter default, spec para 5) neither."""
    if (kdf is None) != (age_recipient is None):
        raise SyncFormatError("kdf and age_recipient must be given together")
    doc = {
        "sync_format": SYNC_FORMAT,
        "library_id": library_id,
        "encryption": "age" if age_recipient else "none",
        "kdf": kdf.to_dict() if kdf else None,
        "age_recipient": age_recipient,
        "created_at": now or _now_iso(),
    }
    return canonical_json(doc) + "\n"


def parse_format_json(text: str) -> SyncFormat:
    try:
        d = json.loads(text)
        if not isinstance(d, dict):
            raise SyncFormatError("format.json is not a JSON object")
        version = d["sync_format"]
        if isinstance(version, bool) or not isinstance(version, int):
            # Strict: build_format_json only ever writes an int, and int()
            # coercion would silently floor a float (S3.3 review Minor #2).
            raise SyncFormatError(f"sync_format must be an integer, got {version!r}")
        if version > SYNC_FORMAT:
            raise SyncFormatError(
                f"backend uses sync_format {version}, this build understands "
                f"{SYNC_FORMAT} — upgrade Tiro on this device before syncing"
            )
        recipient = d.get("age_recipient")
        encryption = d.get("encryption") or ("age" if recipient else "none")
        if encryption not in ("age", "none"):
            # Allowlist (S3.3 review Major #1): an unknown mode must be a
            # clean quarantine-class refusal, never an AttributeError deep
            # in codec construction. A future mode arrives with a
            # sync_format bump anyway, so this costs no forward-compat.
            raise SyncFormatError(f"unknown encryption mode: {encryption!r}")
        kdf = KdfParams.from_dict(d["kdf"]) if d.get("kdf") else None
        if encryption == "age" and (kdf is None or recipient is None):
            raise SyncFormatError("age encryption declared but kdf/recipient missing")
        return SyncFormat(
            sync_format=version,
            library_id=d["library_id"],
            encryption=encryption,
            kdf=kdf,
            age_recipient=recipient,
            created_at=d.get("created_at", ""),
        )
    except (SyncFormatError, CryptoError):
        raise
    except Exception as e:
        raise SyncFormatError(f"unreadable format.json: {e}") from e


def open_format(
    text: str,
    *,
    passphrase: str | None = None,
    recovery_code: str | None = None,
) -> tuple[SyncFormat, PlainCodec | AgeCodec]:
    """Parse + version-check format.json and return the matching codec.
    Wrong passphrase/recovery code -> clean CryptoError HERE, before any
    blob is ever fetched or decrypted (spec para 9 'clean refusal').

    S5 OBLIGATION (whole-branch review Minor #5 — downgrade resistance):
    format.json is PLAINTEXT and unauthenticated, and this function silently
    returns PlainCodec when it declares encryption='none' — even with a
    passphrase supplied. A tampered format.json could therefore flip an
    encrypted library's client to plaintext, and its next PUSH would upload
    unencrypted blobs. The engine must pin the expected encryption mode
    locally (config, set at `tiro sync setup`) and refuse to proceed when
    fmt.encryption differs from the pinned mode."""
    fmt = parse_format_json(text)
    if fmt.encryption == "none":
        return fmt, PlainCodec()
    if recovery_code is not None:
        codec = AgeCodec(recovery_code)
        if codec.recipient != fmt.age_recipient:
            raise CryptoError("recovery code does not match this library")
        return fmt, codec
    if passphrase is not None:
        codec = codec_from_passphrase(passphrase, fmt.kdf)
        if codec.recipient != fmt.age_recipient:
            raise CryptoError("wrong passphrase for this library")
        return fmt, codec
    raise CryptoError("this library is encrypted — passphrase or recovery code required")
