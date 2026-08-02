from pathlib import Path

from imbue.imbue_common.modal_image_requirements import IMAGE_DEPENDENCY_GROUP
from imbue.imbue_common.modal_image_requirements import IMAGE_PINNED_PACKAGE_NAMES
from imbue.imbue_common.modal_image_requirements import image_pinned_app_dir
from imbue.imbue_common.modal_image_requirements import image_requirements_export_command
from imbue.imbue_common.modal_image_requirements import image_requirements_path


def test_pinned_app_dirs_map_package_names_to_app_paths() -> None:
    assert image_pinned_app_dir("remote-service-connector") == "apps/remote_service_connector"
    assert image_pinned_app_dir("modal-litellm") == "apps/modal_litellm"


def test_image_requirements_path_lands_inside_the_app_dir() -> None:
    path = image_requirements_path(Path("/repo"), "modal-litellm")
    assert path == Path("/repo/apps/modal_litellm/image_requirements.txt")


def test_export_command_is_offline_frozen_and_scoped_to_the_image_group() -> None:
    for package_name in IMAGE_PINNED_PACKAGE_NAMES:
        command = image_requirements_export_command(package_name)
        assert "--frozen" in command
        assert "--offline" in command
        assert "--no-header" in command
        assert command[command.index("--package") + 1] == package_name
        assert command[command.index("--only-group") + 1] == IMAGE_DEPENDENCY_GROUP
