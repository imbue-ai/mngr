"""Pure envelope-encryption helpers for password-protected secret sync.

The model: each account has a random 32-byte data-encryption key (the DEK).
Secrets are AEAD-encrypted (AES-256-GCM) directly under the DEK. The DEK
itself is wrapped (also AES-256-GCM) under a key-encryption key (the KEK)
derived from the user's master password with argon2id. Only the *wrapped*
DEK ever leaves the machine; whoever knows the password can unwrap it and
read the secrets, and nobody else (including the storage server) can.

All functions here are pure computations over bytes -- no filesystem or
network access. Key generation uses ``secrets`` (CSPRNG).
"""

import secrets

from argon2.low_level import Type as Argon2Type
from argon2.low_level import hash_secret_raw
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure

# 32 bytes = AES-256 keys for both the KEK and the DEK.
KEY_LENGTH_BYTES = 32
KDF_SALT_LENGTH_BYTES = 16
_AESGCM_NONCE_LENGTH_BYTES = 12

# argon2id parameters following the RFC 9106 low-memory recommendation
# (t=3 iterations, 64 MiB, 4 lanes). Stored alongside every wrapped DEK so
# they can be raised later without breaking existing bundles.
DEFAULT_KDF_TIME_COST = 3
DEFAULT_KDF_MEMORY_KIB = 65536
DEFAULT_KDF_PARALLELISM = 4


class SecretWrappingError(Exception):
    """Base error for the secret-wrapping helpers."""

    ...


class WrongPasswordOrCorruptDataError(SecretWrappingError, ValueError):
    """Raised when an AEAD open fails: wrong password/key, or tampered/corrupt ciphertext."""

    ...


class MalformedCiphertextError(SecretWrappingError, ValueError):
    """Raised when a ciphertext blob is too short to even contain a nonce and tag."""

    ...


class KdfParameters(FrozenModel):
    """The argon2id inputs (except the password) needed to re-derive a KEK."""

    salt: bytes = Field(description="Random per-account salt for argon2id")
    time_cost: int = Field(description="argon2id iteration count")
    memory_kib: int = Field(description="argon2id memory usage in KiB")
    parallelism: int = Field(description="argon2id lane count")


def generate_kdf_parameters() -> KdfParameters:
    """Generate fresh KDF parameters (random salt, current default costs)."""
    return KdfParameters(
        salt=secrets.token_bytes(KDF_SALT_LENGTH_BYTES),
        time_cost=DEFAULT_KDF_TIME_COST,
        memory_kib=DEFAULT_KDF_MEMORY_KIB,
        parallelism=DEFAULT_KDF_PARALLELISM,
    )


def generate_dek() -> bytes:
    """Generate a fresh random 32-byte data-encryption key."""
    return secrets.token_bytes(KEY_LENGTH_BYTES)


@pure
def derive_kek(password: SecretStr, parameters: KdfParameters) -> bytes:
    """Derive the 32-byte key-encryption key from the master password via argon2id.

    Deterministic for a given (password, parameters) pair. An empty password
    is a valid input (the application's default "no master password" state).
    """
    return hash_secret_raw(
        secret=password.get_secret_value().encode("utf-8"),
        salt=parameters.salt,
        time_cost=parameters.time_cost,
        memory_cost=parameters.memory_kib,
        parallelism=parameters.parallelism,
        hash_len=KEY_LENGTH_BYTES,
        type=Argon2Type.ID,
    )


def _aead_encrypt(key: bytes, plaintext: bytes) -> bytes:
    nonce = secrets.token_bytes(_AESGCM_NONCE_LENGTH_BYTES)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, None)


@pure
def _aead_decrypt(key: bytes, blob: bytes) -> bytes:
    """Raises WrongPasswordOrCorruptDataError / MalformedCiphertextError on failure."""
    if len(blob) <= _AESGCM_NONCE_LENGTH_BYTES:
        raise MalformedCiphertextError(f"Ciphertext blob is too short ({len(blob)} bytes) to contain a nonce")
    nonce = blob[:_AESGCM_NONCE_LENGTH_BYTES]
    ciphertext = blob[_AESGCM_NONCE_LENGTH_BYTES:]
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, None)
    except InvalidTag as e:
        raise WrongPasswordOrCorruptDataError("AEAD authentication failed: wrong key or corrupt data") from e


def wrap_dek(kek: bytes, dek: bytes) -> bytes:
    """Encrypt the DEK under the password-derived KEK (nonce-prefixed AES-256-GCM)."""
    return _aead_encrypt(kek, dek)


@pure
def unwrap_dek(kek: bytes, wrapped_dek: bytes) -> bytes:
    """Recover the DEK; a failed authentication tag IS the wrong-password signal.

    Raises WrongPasswordOrCorruptDataError when the password (KEK) is wrong or
    the blob was tampered with.
    """
    return _aead_decrypt(kek, wrapped_dek)


def encrypt_secrets(dek: bytes, plaintext: bytes) -> bytes:
    """Encrypt an opaque secrets payload under the DEK (nonce-prefixed AES-256-GCM)."""
    return _aead_encrypt(dek, plaintext)


@pure
def decrypt_secrets(dek: bytes, blob: bytes) -> bytes:
    """Decrypt a secrets payload. Raises WrongPasswordOrCorruptDataError on tamper/wrong key."""
    return _aead_decrypt(dek, blob)
