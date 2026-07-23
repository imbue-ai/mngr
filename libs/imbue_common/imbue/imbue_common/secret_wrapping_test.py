import pytest
from pydantic import SecretStr

from imbue.imbue_common.secret_wrapping import KDF_SALT_LENGTH_BYTES
from imbue.imbue_common.secret_wrapping import KEY_LENGTH_BYTES
from imbue.imbue_common.secret_wrapping import KdfParameters
from imbue.imbue_common.secret_wrapping import MalformedCiphertextError
from imbue.imbue_common.secret_wrapping import WrongPasswordOrCorruptDataError
from imbue.imbue_common.secret_wrapping import decrypt_secrets
from imbue.imbue_common.secret_wrapping import derive_kek
from imbue.imbue_common.secret_wrapping import encrypt_secrets
from imbue.imbue_common.secret_wrapping import generate_dek
from imbue.imbue_common.secret_wrapping import generate_kdf_parameters
from imbue.imbue_common.secret_wrapping import unwrap_dek
from imbue.imbue_common.secret_wrapping import wrap_dek

# Small argon2 costs so the unit tests stay fast; production costs are the
# module defaults and are exercised once in test_generate_kdf_parameters.
_FAST_PARAMETERS = KdfParameters(salt=b"0123456789abcdef", time_cost=1, memory_kib=8, parallelism=1)


def test_generate_kdf_parameters_uses_defaults_and_random_salt() -> None:
    first = generate_kdf_parameters()
    second = generate_kdf_parameters()

    assert len(first.salt) == KDF_SALT_LENGTH_BYTES
    assert first.salt != second.salt
    assert first.time_cost > 0
    assert first.memory_kib >= 8
    assert first.parallelism > 0


def test_generate_dek_produces_distinct_32_byte_keys() -> None:
    first = generate_dek()
    second = generate_dek()

    assert len(first) == KEY_LENGTH_BYTES
    assert first != second


def test_derive_kek_is_deterministic_for_same_password_and_parameters() -> None:
    first = derive_kek(SecretStr("hunter2"), _FAST_PARAMETERS)
    second = derive_kek(SecretStr("hunter2"), _FAST_PARAMETERS)

    assert first == second
    assert len(first) == KEY_LENGTH_BYTES


def test_derive_kek_differs_for_different_passwords() -> None:
    assert derive_kek(SecretStr("hunter2"), _FAST_PARAMETERS) != derive_kek(SecretStr("hunter3"), _FAST_PARAMETERS)


def test_derive_kek_differs_for_different_salts() -> None:
    other_salt_parameters = KdfParameters(salt=b"fedcba9876543210", time_cost=1, memory_kib=8, parallelism=1)

    assert derive_kek(SecretStr("hunter2"), _FAST_PARAMETERS) != derive_kek(
        SecretStr("hunter2"), other_salt_parameters
    )


def test_empty_password_is_a_valid_kdf_input() -> None:
    kek = derive_kek(SecretStr(""), _FAST_PARAMETERS)

    assert len(kek) == KEY_LENGTH_BYTES


def test_wrap_and_unwrap_dek_round_trips() -> None:
    kek = derive_kek(SecretStr("correct horse"), _FAST_PARAMETERS)
    dek = generate_dek()

    wrapped = wrap_dek(kek, dek)

    assert unwrap_dek(kek, wrapped) == dek


def test_wrapping_twice_produces_different_ciphertexts() -> None:
    kek = derive_kek(SecretStr("correct horse"), _FAST_PARAMETERS)
    dek = generate_dek()

    assert wrap_dek(kek, dek) != wrap_dek(kek, dek)


def test_unwrap_dek_with_wrong_password_raises() -> None:
    dek = generate_dek()
    wrapped = wrap_dek(derive_kek(SecretStr("right"), _FAST_PARAMETERS), dek)
    wrong_kek = derive_kek(SecretStr("wrong"), _FAST_PARAMETERS)

    with pytest.raises(WrongPasswordOrCorruptDataError):
        unwrap_dek(wrong_kek, wrapped)


def test_unwrap_dek_with_tampered_blob_raises() -> None:
    kek = derive_kek(SecretStr("right"), _FAST_PARAMETERS)
    wrapped = wrap_dek(kek, generate_dek())
    tampered = wrapped[:-1] + bytes([wrapped[-1] ^ 0x01])

    with pytest.raises(WrongPasswordOrCorruptDataError):
        unwrap_dek(kek, tampered)


def test_unwrap_dek_with_truncated_blob_raises_malformed() -> None:
    kek = derive_kek(SecretStr("right"), _FAST_PARAMETERS)

    with pytest.raises(MalformedCiphertextError):
        unwrap_dek(kek, b"short")


def test_encrypt_and_decrypt_secrets_round_trips() -> None:
    dek = generate_dek()
    payload = b'{"ssh_private_key": "-----BEGIN...", "restic_env": "RESTIC_REPOSITORY=s3:..."}'

    blob = encrypt_secrets(dek, payload)

    assert blob != payload
    assert decrypt_secrets(dek, blob) == payload


def test_decrypt_secrets_with_wrong_dek_raises() -> None:
    blob = encrypt_secrets(generate_dek(), b"payload")

    with pytest.raises(WrongPasswordOrCorruptDataError):
        decrypt_secrets(generate_dek(), blob)
