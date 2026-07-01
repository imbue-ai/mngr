import base64
import hashlib
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from imbue.minds.errors import LimaImageVerificationError
from imbue.minds.lima_image.minisign_verify import PythonMinisignSignatureVerifier

_KEY_ID = b"\x01\x02\x03\x04\x05\x06\x07\x08"
_TRUSTED_COMMENT = "timestamp:1782580958\tfile:image.raw\thashed"


def _raw_public_bytes(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )


def _write_minisign_fixture(
    tmp_path: Path,
    *,
    content: bytes,
    private_key: Ed25519PrivateKey,
    key_id: bytes = _KEY_ID,
    trusted_comment: str = _TRUSTED_COMMENT,
    tamper_content_after_signing: bytes | None = None,
) -> tuple[Path, Path, str]:
    """Produce (signed_file, signature_file, public_key_line) in real minisign 'ED' (prehashed) format."""
    signed_file = tmp_path / "image.raw"
    signed_file.write_bytes(content)
    digest = hashlib.blake2b(content, digest_size=64).digest()
    signature = private_key.sign(digest)
    global_signature = private_key.sign(signature + trusted_comment.encode())

    sig_blob = base64.b64encode(b"ED" + key_id + signature).decode()
    global_blob = base64.b64encode(global_signature).decode()
    signature_file = tmp_path / "image.raw.minisig"
    signature_file.write_text(
        f"untrusted comment: signature\n{sig_blob}\ntrusted comment: {trusted_comment}\n{global_blob}\n"
    )

    public_key_line = base64.b64encode(b"Ed" + key_id + _raw_public_bytes(private_key)).decode()

    if tamper_content_after_signing is not None:
        signed_file.write_bytes(tamper_content_after_signing)
    return signed_file, signature_file, public_key_line


def test_verifies_a_valid_signature(tmp_path: Path) -> None:
    private_key = Ed25519PrivateKey.generate()
    signed_file, signature_file, public_key = _write_minisign_fixture(
        tmp_path, content=b"the real image bytes" * 100, private_key=private_key
    )
    # Does not raise.
    PythonMinisignSignatureVerifier().verify_detached(
        signed_file=signed_file, signature_file=signature_file, public_key=public_key
    )


def test_rejects_tampered_content(tmp_path: Path) -> None:
    private_key = Ed25519PrivateKey.generate()
    signed_file, signature_file, public_key = _write_minisign_fixture(
        tmp_path,
        content=b"original" * 100,
        private_key=private_key,
        tamper_content_after_signing=b"tampered" * 100,
    )
    with pytest.raises(LimaImageVerificationError):
        PythonMinisignSignatureVerifier().verify_detached(
            signed_file=signed_file, signature_file=signature_file, public_key=public_key
        )


def test_rejects_wrong_public_key(tmp_path: Path) -> None:
    signed_file, signature_file, _ = _write_minisign_fixture(
        tmp_path, content=b"data" * 100, private_key=Ed25519PrivateKey.generate()
    )
    other_key = base64.b64encode(b"Ed" + _KEY_ID + _raw_public_bytes(Ed25519PrivateKey.generate())).decode()
    with pytest.raises(LimaImageVerificationError):
        PythonMinisignSignatureVerifier().verify_detached(
            signed_file=signed_file, signature_file=signature_file, public_key=other_key
        )


def test_rejects_key_id_mismatch(tmp_path: Path) -> None:
    private_key = Ed25519PrivateKey.generate()
    signed_file, signature_file, public_key = _write_minisign_fixture(
        tmp_path, content=b"data" * 100, private_key=private_key
    )
    mismatched_key = base64.b64encode(b"Ed" + b"\x09" * 8 + _raw_public_bytes(private_key)).decode()
    with pytest.raises(LimaImageVerificationError):
        PythonMinisignSignatureVerifier().verify_detached(
            signed_file=signed_file, signature_file=signature_file, public_key=mismatched_key
        )


def test_rejects_tampered_trusted_comment(tmp_path: Path) -> None:
    private_key = Ed25519PrivateKey.generate()
    signed_file, signature_file, public_key = _write_minisign_fixture(
        tmp_path, content=b"data" * 100, private_key=private_key
    )
    # Rewrite the trusted comment line, leaving the (now-stale) global signature.
    lines = signature_file.read_text().splitlines()
    lines[2] = "trusted comment: evil"
    signature_file.write_text("\n".join(lines) + "\n")
    with pytest.raises(LimaImageVerificationError):
        PythonMinisignSignatureVerifier().verify_detached(
            signed_file=signed_file, signature_file=signature_file, public_key=public_key
        )


def test_rejects_truncated_signature_file(tmp_path: Path) -> None:
    (tmp_path / "x.raw").write_bytes(b"data")
    bad_sig = tmp_path / "x.raw.minisig"
    bad_sig.write_text("untrusted comment: only one line\n")
    with pytest.raises(LimaImageVerificationError):
        PythonMinisignSignatureVerifier().verify_detached(
            signed_file=tmp_path / "x.raw",
            signature_file=bad_sig,
            public_key=base64.b64encode(b"Ed" + _KEY_ID + b"\x00" * 32).decode(),
        )
