import base64
import hashlib
from pathlib import Path
from typing import Final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from loguru import logger

from imbue.minds.errors import LimaImageVerificationError
from imbue.minds.lima_image.interfaces import SignatureVerifierInterface

# minisign signature-algorithm tags: "Ed" = legacy (sign the raw file), "ED" =
# prehashed (sign BLAKE2b-512 of the file, minisign's default since 0.6).
_ALG_LEGACY: Final[bytes] = b"Ed"
_ALG_PREHASHED: Final[bytes] = b"ED"
_KEY_ID_LENGTH: Final[int] = 8
_ED25519_SIG_LENGTH: Final[int] = 64
_ED25519_PUBKEY_LENGTH: Final[int] = 32
_BLAKE2B_DIGEST_SIZE: Final[int] = 64
_FILE_READ_CHUNK_BYTES: Final[int] = 1024 * 1024


def _decode_public_key(public_key_line: str) -> tuple[bytes, bytes]:
    """Return (key_id, ed25519_public_key_bytes) parsed from a minisign public-key line."""
    try:
        raw = base64.b64decode(public_key_line.strip(), validate=True)
    except ValueError as exc:
        raise LimaImageVerificationError(f"Malformed minisign public key: {exc}") from exc
    if len(raw) != 2 + _KEY_ID_LENGTH + _ED25519_PUBKEY_LENGTH:
        raise LimaImageVerificationError("minisign public key has unexpected length")
    key_id = raw[2 : 2 + _KEY_ID_LENGTH]
    public_key_bytes = raw[2 + _KEY_ID_LENGTH :]
    return key_id, public_key_bytes


def _parse_signature_file(signature_text: str) -> tuple[bytes, bytes, bytes, bytes, bytes]:
    """Return (alg, key_id, signature, trusted_comment, global_signature) from a minisign .minisig file."""
    lines = signature_text.splitlines()
    if len(lines) < 4:
        raise LimaImageVerificationError("minisign signature file is truncated")
    try:
        sig_blob = base64.b64decode(lines[1].strip(), validate=True)
        global_sig = base64.b64decode(lines[3].strip(), validate=True)
    except ValueError as exc:
        raise LimaImageVerificationError(f"Malformed minisign signature encoding: {exc}") from exc
    if len(sig_blob) != 2 + _KEY_ID_LENGTH + _ED25519_SIG_LENGTH:
        raise LimaImageVerificationError("minisign signature blob has unexpected length")
    if len(global_sig) != _ED25519_SIG_LENGTH:
        raise LimaImageVerificationError("minisign global signature has unexpected length")
    alg = sig_blob[:2]
    key_id = sig_blob[2 : 2 + _KEY_ID_LENGTH]
    signature = sig_blob[2 + _KEY_ID_LENGTH :]
    trusted_comment_prefix = "trusted comment: "
    if not lines[2].startswith(trusted_comment_prefix):
        raise LimaImageVerificationError("minisign signature is missing its trusted comment line")
    trusted_comment = lines[2][len(trusted_comment_prefix) :].encode()
    return alg, key_id, signature, trusted_comment, global_sig


def _signed_message_for_alg(alg: bytes, signed_file: Path) -> bytes:
    """Return the bytes the file signature covers: the raw file (legacy) or its BLAKE2b-512 (prehashed)."""
    if alg == _ALG_PREHASHED:
        digest = hashlib.blake2b(digest_size=_BLAKE2B_DIGEST_SIZE)
        with signed_file.open("rb") as handle:
            for block in iter(lambda: handle.read(_FILE_READ_CHUNK_BYTES), b""):
                digest.update(block)
        return digest.digest()
    if alg == _ALG_LEGACY:
        return signed_file.read_bytes()
    raise LimaImageVerificationError(f"Unsupported minisign signature algorithm: {alg!r}")


class PythonMinisignSignatureVerifier(SignatureVerifierInterface):
    """Verifies detached minisign signatures in pure Python (Ed25519), so no minisign binary is needed at runtime."""

    def verify_detached(self, *, signed_file: Path, signature_file: Path, public_key: str) -> None:
        key_id, public_key_bytes = _decode_public_key(public_key)
        alg, sig_key_id, signature, trusted_comment, global_signature = _parse_signature_file(
            signature_file.read_text()
        )
        if sig_key_id != key_id:
            raise LimaImageVerificationError("minisign signature key id does not match the trusted public key")

        verifier = Ed25519PublicKey.from_public_bytes(public_key_bytes)
        message = _signed_message_for_alg(alg, signed_file)
        try:
            verifier.verify(signature, message)
        except InvalidSignature as exc:
            raise LimaImageVerificationError(f"minisign signature does not verify for {signed_file.name}") from exc
        # The global signature binds the trusted comment to the file signature, so
        # a tampered trusted comment is rejected too.
        try:
            verifier.verify(global_signature, signature + trusted_comment)
        except InvalidSignature as exc:
            raise LimaImageVerificationError("minisign trusted-comment signature does not verify") from exc
        logger.debug("Verified minisign signature (pure Python) for {}", signed_file)
