"""
prompts.py -- versioned, content-hashed prompt registry, owned by the CLIENT.

The client is the source of truth for prompt text. Each file in the prompts
directory is one version (stem = version name); the SHA-256 of its bytes is the
hash we send to the server (as ``prompt_hash``) and record with every review.
"""

from __future__ import annotations

import hashlib
import os
from typing import Optional


class PromptStore:
    def __init__(self, directory: str, active_version: str):
        self.directory = directory
        self.active_version = active_version
        self._by_version: dict[str, dict] = {}
        self.reload()

    def reload(self) -> None:
        loaded: dict[str, dict] = {}
        if not os.path.isdir(self.directory):
            raise FileNotFoundError(f"prompts dir not found: {self.directory}")
        for name in os.listdir(self.directory):
            if name.startswith("."):
                continue
            path = os.path.join(self.directory, name)
            if not os.path.isfile(path):
                continue
            with open(path, "rb") as fh:
                raw = fh.read()
            version = os.path.splitext(name)[0]
            loaded[version] = {
                "version": version,
                "hash": "sha256:" + hashlib.sha256(raw).hexdigest(),
                "text": raw.decode("utf-8"),
            }
        if not loaded:
            raise FileNotFoundError(f"no prompt files in {self.directory}")
        if self.active_version not in loaded:
            raise KeyError(f"active prompt '{self.active_version}' not among {sorted(loaded)}")
        self._by_version = loaded

    def get(self, version: str) -> Optional[dict]:
        return self._by_version.get(version)

    def active(self) -> dict:
        return self._by_version[self.active_version]

    def versions(self) -> list[str]:
        return sorted(self._by_version)
