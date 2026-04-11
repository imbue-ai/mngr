import uuid
from pathlib import Path

from imbue.minds.desktop_client.api_key_store import find_agent_by_api_key
from imbue.minds.desktop_client.api_key_store import generate_api_key
from imbue.minds.desktop_client.api_key_store import hash_api_key
from imbue.minds.desktop_client.api_key_store import save_api_key_hash
from imbue.mngr.primitives import AgentId


def test_generate_api_key_returns_valid_uuid4() -> None:
    key = generate_api_key()
    parsed = uuid.UUID(key)
    assert parsed.version == 4


def test_generate_api_key_returns_unique_keys() -> None:
    key_a = generate_api_key()
    key_b = generate_api_key()
    assert key_a != key_b


def test_hash_api_key_is_deterministic() -> None:
    key = "test-key-12345"
    hash_a = hash_api_key(key)
    hash_b = hash_api_key(key)
    assert hash_a == hash_b


def test_hash_api_key_returns_hex_digest() -> None:
    key = "test-key-12345"
    result = hash_api_key(key)
    assert len(result) == 64
    assert all(c in "0123456789abcdef" for c in result)


def test_hash_api_key_different_keys_different_hashes() -> None:
    hash_a = hash_api_key("key-a")
    hash_b = hash_api_key("key-b")
    assert hash_a != hash_b


def test_save_and_find_api_key_round_trip(tmp_path: Path) -> None:
    agent_id = AgentId()
    key = generate_api_key()
    key_hash = hash_api_key(key)

    save_api_key_hash(tmp_path, agent_id, key_hash)

    found_id = find_agent_by_api_key(tmp_path, key)
    assert found_id == agent_id


def test_find_agent_by_api_key_returns_none_for_unknown_key(tmp_path: Path) -> None:
    agent_id = AgentId()
    key = generate_api_key()
    key_hash = hash_api_key(key)
    save_api_key_hash(tmp_path, agent_id, key_hash)

    wrong_key = generate_api_key()
    found_id = find_agent_by_api_key(tmp_path, wrong_key)
    assert found_id is None


def test_find_agent_by_api_key_returns_none_for_empty_dir(tmp_path: Path) -> None:
    found_id = find_agent_by_api_key(tmp_path, "any-key")
    assert found_id is None


def test_find_agent_by_api_key_with_multiple_agents(tmp_path: Path) -> None:
    agent_a = AgentId()
    key_a = generate_api_key()
    save_api_key_hash(tmp_path, agent_a, hash_api_key(key_a))

    agent_b = AgentId()
    key_b = generate_api_key()
    save_api_key_hash(tmp_path, agent_b, hash_api_key(key_b))

    assert find_agent_by_api_key(tmp_path, key_a) == agent_a
    assert find_agent_by_api_key(tmp_path, key_b) == agent_b


def test_save_api_key_hash_creates_directory_structure(tmp_path: Path) -> None:
    agent_id = AgentId()
    save_api_key_hash(tmp_path, agent_id, "test-hash")

    hash_file = tmp_path / "agents" / str(agent_id) / "api_key_hash"
    assert hash_file.exists()
    assert hash_file.read_text() == "test-hash"


def test_find_agent_by_api_key_handles_corrupted_hash_file(tmp_path: Path) -> None:
    """Verify that a non-readable hash file is skipped without crashing."""
    agents_dir = tmp_path / "agents" / "fake-agent"
    agents_dir.mkdir(parents=True)
    hash_file = agents_dir / "api_key_hash"
    # Write a valid hash file for a different key
    hash_file.write_text(hash_api_key("other-key"))

    found_id = find_agent_by_api_key(tmp_path, "some-key")
    assert found_id is None
