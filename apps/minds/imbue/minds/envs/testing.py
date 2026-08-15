"""Shared non-fixture test utilities for the envs package."""


def make_workspace_storage_vault_values() -> dict[str, str]:
    """A fully-populated fake ``storage`` Vault entry (every key workspace-storage needs)."""
    return {
        "WORKSPACE_STORAGE_S3_ENDPOINT": "https://s3.example.com",
        "WORKSPACE_STORAGE_S3_REGION": "us-east-va",
        "WORKSPACE_STORAGE_S3_ACCESS_KEY": "fake-access-key",
        "WORKSPACE_STORAGE_S3_SECRET_KEY": "fake-secret-key",
        "WORKSPACE_STORAGE_BUCKET": "mngr-workspaces-test",
    }
