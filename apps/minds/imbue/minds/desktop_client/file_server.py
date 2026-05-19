"""File server endpoints exposed under ``/api/v1/file-server``.

Provides three filesystem operations to authenticated API callers:

* ``GET  /api/v1/file-server?path=<absolute_path>&operation=READ`` --
  stream the contents of a regular file.
* ``GET  /api/v1/file-server?path=<absolute_path>&operation=LIST`` --
  return a JSON listing of a directory's entries with per-entry
  metadata.
* ``GET  /api/v1/file-server?path=<absolute_path>&operation=STAT`` --
  return JSON metadata for a single path (file, directory, or symlink).
* ``POST /api/v1/file-server?path=<absolute_path>[&overwrite=true]`` --
  write the raw request body to the target file. Refuses with ``409``
  when the file already exists unless ``overwrite=true`` is set. Parent
  directories are created on demand.

Authentication piggy-backs on the same per-agent Bearer-token check
that gates the rest of ``/api/v1/...`` (see ``api_v1.py``). The
file-server itself does NOT sandbox or otherwise restrict which paths
a caller may read or write -- the intent is that the
``minds-api-proxy`` Latchkey gateway extension is reached through a
``latchkey_permissions.json`` rule that constrains what an agent can
ask for, and the bearer-token check stops untrusted callers entirely.
"""

import os
import stat as stat_module
from datetime import datetime
from datetime import timezone
from enum import auto
from pathlib import Path
from typing import Annotated
from typing import assert_never

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi.responses import FileResponse
from fastapi.responses import Response
from loguru import logger
from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.desktop_client.api_key_auth import CallerAgentIdDep


class FileServerOperation(UpperCaseStrEnum):
    """Operation selector for ``GET /api/v1/file-server``."""

    READ = auto()
    LIST = auto()
    STAT = auto()


class FileEntryType(UpperCaseStrEnum):
    """Kind of filesystem entry returned by LIST / STAT."""

    FILE = auto()
    DIRECTORY = auto()
    SYMLINK = auto()
    OTHER = auto()


class FileMetadata(FrozenModel):
    """Filesystem metadata for a single path."""

    name: str = Field(description="Entry basename when returned inside a LIST, otherwise the requested absolute path")
    type: FileEntryType = Field(description="Kind of filesystem entry, classified via lstat()")
    size_bytes: int = Field(description="Size in bytes as reported by lstat()")
    modified_at: datetime = Field(description="Last modification time (UTC, second precision)")


class DirectoryListing(FrozenModel):
    """Result of a LIST operation on a directory."""

    path: str = Field(description="Absolute path of the directory that was listed")
    entries: tuple[FileMetadata, ...] = Field(
        description="Entries inside the directory, sorted alphabetically by name"
    )


class WriteResult(FrozenModel):
    """Successful response payload for ``POST /api/v1/file-server``."""

    path: str = Field(description="Absolute path of the file that was written")
    bytes_written: int = Field(description="Number of bytes written to disk")


def _classify_st_mode(mode: int) -> FileEntryType:
    if stat_module.S_ISLNK(mode):
        return FileEntryType.SYMLINK
    if stat_module.S_ISDIR(mode):
        return FileEntryType.DIRECTORY
    if stat_module.S_ISREG(mode):
        return FileEntryType.FILE
    return FileEntryType.OTHER


def _build_metadata(name: str, st: os.stat_result) -> FileMetadata:
    return FileMetadata(
        name=name,
        type=_classify_st_mode(st.st_mode),
        size_bytes=st.st_size,
        modified_at=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
    )


def _model_response(model: FrozenModel, status_code: int = 200) -> Response:
    return Response(
        content=model.model_dump_json(),
        media_type="application/json",
        status_code=status_code,
    )


def _validated_absolute_path(path_str: str) -> Path:
    if not path_str:
        raise HTTPException(status_code=400, detail="'path' query parameter is required")
    target = Path(path_str)
    if not target.is_absolute():
        raise HTTPException(status_code=400, detail=f"'path' must be absolute (got: {path_str!r})")
    return target


def _lstat_or_raise(target_path: Path) -> os.stat_result:
    """Run ``lstat`` and translate common errno cases into HTTPException."""
    try:
        return target_path.lstat()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Path not found: {target_path}") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=f"Permission denied: {target_path}") from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Cannot stat path: {exc}") from exc


def _handle_read(target_path: Path) -> Response:
    st = _lstat_or_raise(target_path)
    if not stat_module.S_ISREG(st.st_mode):
        raise HTTPException(status_code=400, detail=f"Path is not a regular file: {target_path}")
    # FileResponse streams the file body and sets Content-Length from
    # stat(), so even very large files do not get loaded into memory.
    return FileResponse(target_path, media_type="application/octet-stream")


def _handle_list(target_path: Path) -> Response:
    st = _lstat_or_raise(target_path)
    if not stat_module.S_ISDIR(st.st_mode):
        raise HTTPException(status_code=400, detail=f"Path is not a directory: {target_path}")
    try:
        entry_names = sorted(os.listdir(target_path))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=f"Permission denied: {target_path}") from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Cannot list directory: {exc}") from exc

    # Tolerate per-entry stat failures (races, restricted symlink targets,
    # ...): skip the entry rather than failing the whole listing.
    entries: list[FileMetadata] = []
    for name in entry_names:
        entry_path = target_path / name
        try:
            entry_st = entry_path.lstat()
        except OSError as exc:
            logger.debug("Skipping unstattable directory entry {}: {}", entry_path, exc)
            continue
        entries.append(_build_metadata(name=name, st=entry_st))
    listing = DirectoryListing(path=str(target_path), entries=tuple(entries))
    return _model_response(listing)


def _handle_stat(target_path: Path) -> Response:
    st = _lstat_or_raise(target_path)
    return _model_response(_build_metadata(name=str(target_path), st=st))


def _handle_file_server_get(
    _caller_agent_id: CallerAgentIdDep,
    path: Annotated[str, Query(description="Absolute filesystem path on the host")],
    operation: Annotated[
        FileServerOperation,
        Query(description="One of READ (default), LIST, or STAT. Case-sensitive, upper-case."),
    ] = FileServerOperation.READ,
) -> Response:
    """Read a file, list a directory, or fetch filesystem metadata."""
    target_path = _validated_absolute_path(path)
    match operation:
        case FileServerOperation.READ:
            return _handle_read(target_path)
        case FileServerOperation.LIST:
            return _handle_list(target_path)
        case FileServerOperation.STAT:
            return _handle_stat(target_path)
        case _ as unreachable:
            assert_never(unreachable)


async def _handle_file_server_post(
    request: Request,
    _caller_agent_id: CallerAgentIdDep,
    path: Annotated[str, Query(description="Absolute filesystem path of the file to write")],
    overwrite: Annotated[
        bool,
        Query(description="If true, replace an existing regular file at the target path."),
    ] = False,
) -> Response:
    """Write the raw request body to ``path``.

    By default, refuses with ``409`` when the target already exists.
    Pass ``overwrite=true`` to replace an existing regular file. Parent
    directories are created on demand. The body is written verbatim as
    bytes -- no JSON parsing or text decoding occurs.
    """
    target_path = _validated_absolute_path(path)
    # ``exists()`` follows symlinks, which is what we want: writing
    # through a dangling symlink would still create the target file, so
    # we must refuse unless ``overwrite=true``.
    if target_path.exists() and not overwrite:
        raise HTTPException(
            status_code=409,
            detail=f"File already exists: {target_path}. Pass overwrite=true to replace it.",
        )
    if target_path.is_dir():
        raise HTTPException(status_code=400, detail=f"Cannot write to a directory: {target_path}")
    body = await request.body()
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=f"Permission denied writing to {target_path}") from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Cannot write file: {exc}") from exc
    return _model_response(WriteResult(path=str(target_path), bytes_written=len(body)))


def register_file_server_routes(router: APIRouter) -> None:
    """Attach the file-server GET and POST handlers to ``router``."""
    router.get("/file-server")(_handle_file_server_get)
    router.post("/file-server")(_handle_file_server_post)
