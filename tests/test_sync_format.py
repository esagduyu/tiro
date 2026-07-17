"""Sync S3: format.json — the plaintext root document (spec para 5)."""
import json

import pytest

from tiro.sync.crypto import (
    AgeCodec,
    CryptoError,
    KdfParams,
    PlainCodec,
    SyncFormatError,
    build_format_json,
    codec_from_passphrase,
    new_kdf_params,
    open_format,
    parse_format_json,
)

KDF = KdfParams(salt_b64=new_kdf_params().salt_b64, m=8, t=1, p=1)
LIB = "01JLIB0000000000000000TEST"


def _age_format_text(passphrase="hunter2"):
    codec = codec_from_passphrase(passphrase, KDF)
    return build_format_json(LIB, kdf=KDF, age_recipient=codec.recipient,
                             now="2026-07-11T00:00:00Z")


class TestBuildParse:
    def test_age_roundtrip_and_field_names(self):
        text = _age_format_text()
        d = json.loads(text)
        # Spec para 5 field names, frozen.
        assert set(d) == {"sync_format", "library_id", "encryption", "kdf",
                          "age_recipient", "created_at"}
        assert d["sync_format"] == 1
        assert d["kdf"]["algo"] == "argon2id"
        assert set(d["kdf"]) == {"algo", "salt", "m", "t", "p"}
        fmt = parse_format_json(text)
        assert fmt.library_id == LIB
        assert fmt.encryption == "age"
        assert fmt.kdf == KDF

    def test_plaintext_mode(self):
        text = build_format_json(LIB, now="2026-07-11T00:00:00Z")
        d = json.loads(text)
        assert d["encryption"] == "none"
        assert d["kdf"] is None and d["age_recipient"] is None
        fmt = parse_format_json(text)
        assert fmt.encryption == "none" and fmt.kdf is None

    def test_parse_infers_encryption_when_field_missing(self):
        """Forward-tolerant read (decision #5)."""
        d = json.loads(_age_format_text())
        del d["encryption"]
        assert parse_format_json(json.dumps(d)).encryption == "age"

    def test_garbage_is_sync_format_error(self):
        for bad in ("not json", "[]", '{"sync_format": 1}'):
            with pytest.raises(SyncFormatError):
                parse_format_json(bad)


class TestOpenFormat:
    def test_open_with_passphrase(self):
        fmt, codec = open_format(_age_format_text(), passphrase="hunter2")
        assert isinstance(codec, AgeCodec)
        assert codec.recipient == fmt.age_recipient

    def test_wrong_passphrase_clean_refusal(self):
        """Spec para 9 scenario: wrong passphrase -> clean refusal BEFORE any
        blob is touched (recipient mismatch at format-open, decision #13)."""
        with pytest.raises(CryptoError, match="passphrase"):
            open_format(_age_format_text(), passphrase="wrong")

    def test_open_with_recovery_code(self):
        from tiro.sync.crypto import derive_recovery_code
        fmt, codec = open_format(_age_format_text(),
                                 recovery_code=derive_recovery_code("hunter2", KDF))
        assert codec.recipient == fmt.age_recipient

    def test_mismatched_recovery_code_refused(self):
        from tiro.sync.crypto import derive_recovery_code
        with pytest.raises(CryptoError):
            open_format(_age_format_text(),
                        recovery_code=derive_recovery_code("other", KDF))

    def test_age_without_credentials_refused(self):
        with pytest.raises(CryptoError, match="passphrase or recovery code"):
            open_format(_age_format_text())

    def test_plaintext_needs_no_credentials(self):
        fmt, codec = open_format(build_format_json(LIB))
        assert isinstance(codec, PlainCodec)

    def test_newer_format_refused_with_upgrade_prompt(self):
        """Roadmap rule: sync_format newer than this build -> refuse, tell the
        user to upgrade Tiro (never attempt a partial read)."""
        d = json.loads(_age_format_text())
        d["sync_format"] = 2
        with pytest.raises(SyncFormatError, match="[Uu]pgrade"):
            open_format(json.dumps(d), passphrase="hunter2")
        with pytest.raises(SyncFormatError, match="[Uu]pgrade"):
            parse_format_json(json.dumps(d))
