from pathlib import Path

from imbue.mngr_lima.lima_yaml import generate_default_lima_yaml
from imbue.mngr_lima.lima_yaml import load_user_lima_yaml
from imbue.mngr_lima.lima_yaml import merge_lima_yaml
from imbue.mngr_lima.lima_yaml import parse_build_args_for_yaml_path
from imbue.mngr_lima.lima_yaml import write_lima_yaml


def test_generate_default_lima_yaml(tmp_path: Path) -> None:
    volume_path = tmp_path / "volume"
    volume_path.mkdir()

    config = generate_default_lima_yaml(
        volume_host_path=volume_path,
        host_dir="/mngr",
    )

    assert "images" in config
    assert len(config["images"]) == 1
    assert "location" in config["images"][0]
    assert "arch" in config["images"][0]

    assert "mounts" in config
    assert len(config["mounts"]) == 1
    assert config["mounts"][0]["mountPoint"] == "/mngr"
    assert config["mounts"][0]["writable"] is True

    assert "provision" in config
    assert len(config["provision"]) == 1
    assert config["provision"][0]["mode"] == "system"


def test_generate_default_lima_yaml_custom_image(tmp_path: Path) -> None:
    volume_path = tmp_path / "volume"
    volume_path.mkdir()

    config = generate_default_lima_yaml(
        volume_host_path=volume_path,
        host_dir="/mngr",
        custom_image_url="https://example.com/custom.qcow2",
    )

    assert config["images"][0]["location"] == "https://example.com/custom.qcow2"


def test_write_lima_yaml(tmp_path: Path) -> None:
    config = {"images": [{"location": "test.qcow2", "arch": "x86_64"}]}
    output_path = tmp_path / "test.yaml"
    result = write_lima_yaml(config, output_path)
    assert result == output_path
    assert output_path.exists()
    content = output_path.read_text()
    assert "test.qcow2" in content


def test_write_lima_yaml_temp_file() -> None:
    config = {"images": [{"location": "test.qcow2"}]}
    result = write_lima_yaml(config)
    assert result.exists()
    assert result.suffix == ".yaml"
    # Clean up
    result.unlink()


def test_load_user_lima_yaml(tmp_path: Path) -> None:
    yaml_path = tmp_path / "user.yaml"
    yaml_path.write_text("cpus: 8\nmemory: 16GiB\n")
    config = load_user_lima_yaml(yaml_path)
    assert config["cpus"] == 8
    assert config["memory"] == "16GiB"


def test_merge_lima_yaml() -> None:
    base = {"images": [{"location": "default.qcow2"}], "cpus": 4}
    override = {"cpus": 8, "memory": "16GiB"}
    merged = merge_lima_yaml(base, override)
    assert merged["cpus"] == 8
    assert merged["memory"] == "16GiB"
    assert merged["images"] == [{"location": "default.qcow2"}]


def test_parse_build_args_for_yaml_path() -> None:
    assert parse_build_args_for_yaml_path(("--file", "/path/to/config.yaml")) == Path("/path/to/config.yaml")
    assert parse_build_args_for_yaml_path(("--file=/path/to/config.yaml",)) == Path("/path/to/config.yaml")
    assert parse_build_args_for_yaml_path(("--other", "arg")) is None
    assert parse_build_args_for_yaml_path(()) is None
