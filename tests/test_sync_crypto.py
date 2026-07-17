"""Sync S3: crypto core — Argon2id -> X25519 age identity, recovery code, codecs.

The parity fixture test (TestParityFixture) is added in S3.2 and appended to
this file; this module starts with behavior tests only."""
import base64

import pytest

from tiro.sync.crypto import (
    AgeCodec,
    CryptoError,
    KdfParams,
    PlainCodec,
    codec_from_passphrase,
    derive_recovery_code,
    derive_secret_key,
    new_kdf_params,
    secret_key_to_recovery_code,
)

# Small params: fast (<10ms) and legal (argon2 requires m >= 8*p).
SMALL = KdfParams(salt_b64=base64.b64encode(b"\x01" * 16).decode(), m=8, t=1, p=1)


class TestKdf:
    def test_defaults_are_rfc9106_low_memory(self):
        k = new_kdf_params()
        assert (k.m, k.t, k.p, k.algo) == (65536, 3, 4, "argon2id")
        assert len(base64.b64decode(k.salt_b64)) == 16

    def test_dict_roundtrip(self):
        k = new_kdf_params()
        assert KdfParams.from_dict(k.to_dict()) == k
        assert k.to_dict()["algo"] == "argon2id"

    def test_from_dict_rejects_unknown_algo(self):
        with pytest.raises(CryptoError):
            KdfParams.from_dict({"algo": "scrypt", "salt": SMALL.salt_b64, "m": 8, "t": 1, "p": 1})

    def test_derivation_deterministic_and_param_sensitive(self):
        a = derive_secret_key("pass", SMALL)
        assert a == derive_secret_key("pass", SMALL)
        assert len(a) == 32
        assert a != derive_secret_key("PASS", SMALL)
        other_salt = KdfParams(salt_b64=base64.b64encode(b"\x02" * 16).decode(), m=8, t=1, p=1)
        assert a != derive_secret_key("pass", other_salt)
        assert a != derive_secret_key("pass", KdfParams(salt_b64=SMALL.salt_b64, m=8, t=2, p=1))

    def test_bad_salt_is_crypto_error(self):
        with pytest.raises(CryptoError):
            derive_secret_key("pass", KdfParams(salt_b64="not base64!!!", m=8, t=1, p=1))


class TestRecoveryCode:
    def test_shape(self):
        code = secret_key_to_recovery_code(b"\x00" * 32)
        assert code.startswith("AGE-SECRET-KEY-1")
        assert code == code.upper()

    def test_age_accepts_our_bech32(self):
        """from_str validates HRP + Bech32 checksum — this proves our encoder
        emits exactly age's own key format (decision #2)."""
        import secrets as _secrets
        for _ in range(4):
            code = secret_key_to_recovery_code(_secrets.token_bytes(32))
            AgeCodec(code)  # raises CryptoError if the encoding were wrong

    def test_derive_recovery_code_deterministic(self):
        assert derive_recovery_code("pass", SMALL) == derive_recovery_code("pass", SMALL)


class TestCodecs:
    def test_age_roundtrip(self):
        codec = codec_from_passphrase("hunter2", SMALL)
        blob = codec.encrypt(b"secret body \xf0\x9f\x93\x9a")
        assert blob != b"secret body \xf0\x9f\x93\x9a"
        assert codec.decrypt(blob) == b"secret body \xf0\x9f\x93\x9a"
        assert codec.encryption == "age"
        assert codec.recipient.startswith("age1")

    def test_recovery_code_equals_passphrase_identity(self):
        """Pairing with the recovery code decrypts what the passphrase wrote."""
        writer = codec_from_passphrase("hunter2", SMALL)
        code = derive_recovery_code("hunter2", SMALL)
        blob = writer.encrypt(b"x")
        assert AgeCodec(code).decrypt(blob) == b"x"

    def test_wrong_identity_is_clean_crypto_error(self):
        blob = codec_from_passphrase("right", SMALL).encrypt(b"x")
        with pytest.raises(CryptoError):
            codec_from_passphrase("wrong", SMALL).decrypt(blob)

    def test_invalid_recovery_code_is_crypto_error(self):
        with pytest.raises(CryptoError):
            AgeCodec("AGE-SECRET-KEY-1NOTAVALIDCODE")
        with pytest.raises(CryptoError):
            AgeCodec("garbage")

    def test_plain_codec_is_identity(self):
        c = PlainCodec()
        assert c.encrypt(b"abc") == b"abc"
        assert c.decrypt(b"abc") == b"abc"
        assert c.encryption == "none"
