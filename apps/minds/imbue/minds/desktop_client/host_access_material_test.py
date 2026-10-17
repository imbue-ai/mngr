from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.asymmetric import rsa

from imbue.minds.desktop_client.host_access_material import HostAccessLayout
from imbue.minds.desktop_client.host_access_material import HostAccessMaterial
from imbue.minds.desktop_client.host_access_material import collect_host_access_material
from imbue.minds.desktop_client.host_access_material import derive_openssh_public_key_line
from imbue.minds.desktop_client.host_access_material import host_access_paths
from imbue.minds.desktop_client.host_access_material import is_host_access_material_missing_locally
from imbue.minds.desktop_client.host_access_material import layout_for_provider_kind
from imbue.minds.desktop_client.host_access_material import materialize_host_access_material
from imbue.minds.desktop_client.testing import generate_test_ssh_private_key
from imbue.mngr.primitives import HostId
from imbue.mngr.providers.host_key_store import HostKeyOrigin
from imbue.mngr.providers.host_key_store import load_host_key_record
from imbue.mngr.providers.host_key_store import pin_host_key


@pytest.mark.parametrize(
    ("provider_kind", "expected"),
    [
        ("imbue_cloud_3f2a9c1e-5b7d-4a8e-9c0f-1d2e3f4a5b6c", HostAccessLayout.IMBUE_CLOUD),
        ("imbue_cloud_alice-example-com", HostAccessLayout.IMBUE_CLOUD),
        ("aws_" + "0" * 32, HostAccessLayout.VPS),
        ("gcp_" + "1" * 32, HostAccessLayout.VPS),
        ("azure_" + "2" * 32, HostAccessLayout.VPS),
        ("vultr", HostAccessLayout.VPS),
        ("modal_" + "3" * 32, HostAccessLayout.MODAL),
        ("lima", HostAccessLayout.LIMA),
        ("docker", HostAccessLayout.DOCKER),
        ("local", None),
        ("aws", None),
        ("", None),
    ],
)
def test_layout_for_provider_kind(provider_kind: str, expected: HostAccessLayout | None) -> None:
    assert layout_for_provider_kind(provider_kind) is expected


def test_vps_paths_follow_the_per_instance_key_dir(tmp_path: Path) -> None:
    host_id = HostId.generate()
    instance = f"aws_{uuid4().hex}"

    paths = host_access_paths(tmp_path, instance, host_id)

    assert paths is not None
    key_dir = tmp_path / "providers" / "aws" / instance / "keys"
    assert paths.per_host_dir == key_dir / "host_keys" / host_id.get_uuid().hex
    assert paths.pin_store_by_name == {
        "vps_known_hosts": key_dir / "vps_known_hosts",
        "container_known_hosts": key_dir / "container_known_hosts",
    }
    assert paths.agent_key_file_name == "vps_ssh_key"


def test_modal_paths_share_the_vps_shape(tmp_path: Path) -> None:
    host_id = HostId.generate()
    instance = f"modal_{uuid4().hex}"

    paths = host_access_paths(tmp_path, instance, host_id)

    assert paths is not None
    key_dir = tmp_path / "providers" / "modal" / instance / "keys"
    assert paths.per_host_dir == key_dir / "host_keys" / host_id.get_uuid().hex
    assert paths.pin_store_by_name == {"known_hosts": key_dir / "known_hosts"}


def test_imbue_cloud_and_lima_paths_keep_their_per_host_stores(tmp_path: Path) -> None:
    host_id = HostId.generate()
    imbue_cloud_paths = host_access_paths(tmp_path, f"imbue_cloud_{uuid4()}", host_id)
    lima_paths = host_access_paths(tmp_path, "lima", host_id)

    assert imbue_cloud_paths is not None and lima_paths is not None
    assert imbue_cloud_paths.pin_store_by_name == {"known_hosts": imbue_cloud_paths.per_host_dir / "known_hosts"}
    assert imbue_cloud_paths.is_pin_store_per_host
    assert lima_paths.per_host_dir == tmp_path / "providers" / "lima" / "lima" / "keys" / "hosts" / str(host_id)
    assert lima_paths.file_names == (
        "ssh_host_ed25519_key",
        "ssh_host_ed25519_key.pub",
        "root_ssh_key",
        "root_ssh_key.pub",
    )


def test_docker_paths_share_the_modal_shape_and_collect_the_per_host_material(tmp_path: Path) -> None:
    """docker mints per-host client and host keypairs under its instance key dir, exactly like modal."""
    host_id = HostId.generate()
    paths = host_access_paths(tmp_path, "docker", host_id)

    assert paths is not None
    key_dir = tmp_path / "providers" / "docker" / "docker" / "keys"
    assert paths.per_host_dir == key_dir / "host_keys" / host_id.get_uuid().hex
    assert paths.agent_key_file_name == "docker_ssh_key"
    assert paths.pin_store_by_name == {"known_hosts": key_dir / "known_hosts"}
    assert collect_host_access_material(paths) is None
    paths.per_host_dir.mkdir(parents=True)
    (paths.per_host_dir / "docker_ssh_key").write_text(generate_test_ssh_private_key())
    (paths.per_host_dir / "host_key").write_text("DOCKER-HOST-KEY")
    pin_host_key(
        paths.pin_store_by_name["known_hosts"], "127.0.0.1", 2222, "ssh-ed25519 AAAA-c", host_id, HostKeyOrigin.USER
    )

    material = collect_host_access_material(paths)

    assert material is not None
    assert material.layout == "docker"
    # The client key's missing public half is derived; the host key has no derivable half here.
    assert set(material.files) == {"docker_ssh_key", "docker_ssh_key.pub", "host_key"}
    assert material.files["docker_ssh_key.pub"].startswith("ssh-ed25519 ")
    assert material.pins == {"known_hosts": "[127.0.0.1]:2222 ssh-ed25519 AAAA-c\n"}


def test_lima_legacy_root_key_links_are_neither_collected_nor_written_through(tmp_path: Path) -> None:
    """mngr_lima links a healed legacy host's root key slots to the provider-wide key, which must stay off the record."""
    host_id = HostId.generate()
    paths = host_access_paths(tmp_path, "lima", host_id)
    assert paths is not None
    keys_dir = paths.per_host_dir.parent.parent
    paths.per_host_dir.mkdir(parents=True)
    legacy_private = generate_test_ssh_private_key()
    (keys_dir / "root_ssh_key").write_text(legacy_private)
    (keys_dir / "root_ssh_key.pub").write_text("ssh-ed25519 AAAA-legacy")
    for file_name in ("root_ssh_key", "root_ssh_key.pub"):
        (paths.per_host_dir / file_name).symlink_to(Path("..") / ".." / file_name)
    (paths.per_host_dir / "ssh_host_ed25519_key").write_text(generate_test_ssh_private_key())

    material = collect_host_access_material(paths)

    assert material is not None
    assert set(material.files) == {"ssh_host_ed25519_key", "ssh_host_ed25519_key.pub"}

    synced = HostAccessMaterial(layout="lima", files={**material.files, "root_ssh_key": "SYNCED-KEY"}, pins={})
    materialize_host_access_material(paths, synced)

    assert (keys_dir / "root_ssh_key").read_text() == legacy_private
    assert (paths.per_host_dir / "root_ssh_key").is_symlink()


def test_collect_reads_only_the_target_hosts_records_from_a_provider_wide_store(tmp_path: Path) -> None:
    """A vps pin store holds every host's pins; the record must carry only this host's."""
    instance = f"aws_{uuid4().hex}"
    host_id = HostId.generate()
    other_host_id = HostId.generate()
    paths = host_access_paths(tmp_path, instance, host_id)
    assert paths is not None
    paths.per_host_dir.mkdir(parents=True)
    (paths.per_host_dir / "vps_ssh_key").write_text("VPS-KEY")
    (paths.per_host_dir / "vps_ssh_key.pub").write_text("VPS-KEY-PUB")
    (paths.per_host_dir / "container_ssh_key").write_text("CONTAINER-KEY")
    (paths.per_host_dir / "stray_file").write_text("never collected")
    vps_store = paths.pin_store_by_name["vps_known_hosts"]
    container_store = paths.pin_store_by_name["container_known_hosts"]
    pin_host_key(vps_store, "10.0.0.5", 22, "ssh-ed25519 AAAA-vps", host_id, HostKeyOrigin.USER)
    pin_host_key(vps_store, "10.0.0.9", 22, "ssh-ed25519 AAAA-other", other_host_id, HostKeyOrigin.USER)
    pin_host_key(container_store, "10.0.0.5", 2222, "ssh-ed25519 AAAA-container", host_id, HostKeyOrigin.USER)

    material = collect_host_access_material(paths)

    assert material is not None
    assert material.layout == "vps"
    assert material.files == {
        "vps_ssh_key": "VPS-KEY",
        "vps_ssh_key.pub": "VPS-KEY-PUB",
        "container_ssh_key": "CONTAINER-KEY",
    }
    assert material.pins == {
        # The default port renders without the bracket form; the container port keeps it.
        "vps_known_hosts": "10.0.0.5 ssh-ed25519 AAAA-vps\n",
        "container_known_hosts": "[10.0.0.5]:2222 ssh-ed25519 AAAA-container\n",
    }


def test_materialize_lays_the_material_down_and_is_idempotent(tmp_path: Path) -> None:
    instance = f"modal_{uuid4().hex}"
    host_id = HostId.generate()
    paths = host_access_paths(tmp_path, instance, host_id)
    assert paths is not None
    material = HostAccessMaterial(
        layout="modal",
        files={"modal_ssh_key": "MODAL-KEY", "modal_ssh_key.pub": "MODAL-KEY-PUB", "host_key": "HOST-KEY"},
        pins={"known_hosts": "[sandbox.modal.host]:2222 ssh-ed25519 AAAA-sandbox\n"},
    )
    assert is_host_access_material_missing_locally(paths, material)

    assert materialize_host_access_material(paths, material) is True

    assert (paths.per_host_dir / "modal_ssh_key").read_text() == "MODAL-KEY"
    assert oct((paths.per_host_dir / "modal_ssh_key").stat().st_mode & 0o777) == oct(0o600)
    assert oct((paths.per_host_dir / "modal_ssh_key.pub").stat().st_mode & 0o777) == oct(0o644)
    assert (paths.per_host_dir / "known_hosts").is_symlink()
    assert (paths.per_host_dir / "known_hosts").resolve() == paths.pin_store_by_name["known_hosts"].resolve()
    record = load_host_key_record(paths.pin_store_by_name["known_hosts"], host_id)
    assert record is not None and [pin.public_key for pin in record.pins] == ["ssh-ed25519 AAAA-sandbox"]
    assert not is_host_access_material_missing_locally(paths, material)
    # A second application changes nothing.
    assert materialize_host_access_material(paths, material) is False
    # The material round-trips through the collector.
    assert collect_host_access_material(paths) == material


def test_materialize_replaces_a_bootstrap_pin_with_the_records_user_pin(tmp_path: Path) -> None:
    instance = f"gcp_{uuid4().hex}"
    host_id = HostId.generate()
    paths = host_access_paths(tmp_path, instance, host_id)
    assert paths is not None
    store = paths.pin_store_by_name["vps_known_hosts"]
    pin_host_key(store, "10.1.1.1", 22, "ssh-ed25519 AAAA-bootstrap", host_id, HostKeyOrigin.BOOTSTRAP)
    material = HostAccessMaterial(
        layout="vps", files={}, pins={"vps_known_hosts": "[10.1.1.1]:22 ssh-ed25519 AAAA-rotated\n"}
    )
    assert is_host_access_material_missing_locally(paths, material)

    assert materialize_host_access_material(paths, material) is True

    record = load_host_key_record(store, host_id)
    assert record is not None and [pin.public_key for pin in record.pins] == ["ssh-ed25519 AAAA-rotated"]


def test_derive_openssh_public_key_line_roundtrips_an_openssh_format_key() -> None:
    private_key = ed25519.Ed25519PrivateKey.generate()
    private_text = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    expected_public = (
        private_key.public_key()
        .public_bytes(encoding=serialization.Encoding.OpenSSH, format=serialization.PublicFormat.OpenSSH)
        .decode("utf-8")
    )

    assert derive_openssh_public_key_line(private_text) == expected_public


def test_derive_openssh_public_key_line_roundtrips_mngrs_traditional_pem_rsa_key() -> None:
    # The legacy flavor older mngr installs wrote for client keys (mngr now
    # generates Ed25519): RSA in traditional PEM ("-----BEGIN RSA PRIVATE
    # KEY-----"), which needs the PEM loader, not the OpenSSH one. Records
    # synced from those installs still carry these keys.
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_text = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    expected_public = (
        private_key.public_key()
        .public_bytes(encoding=serialization.Encoding.OpenSSH, format=serialization.PublicFormat.OpenSSH)
        .decode("utf-8")
    )

    assert private_text.startswith("-----BEGIN RSA PRIVATE KEY-----")
    assert derive_openssh_public_key_line(private_text) == expected_public


def test_derive_openssh_public_key_line_returns_none_for_garbage() -> None:
    assert derive_openssh_public_key_line("not a key at all") is None
