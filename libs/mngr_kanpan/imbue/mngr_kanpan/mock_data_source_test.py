"""Shared configurable fake implementing the KanpanDataSource protocol.

Used by fetcher_test.py and test_fetcher_acceptance.py instead of each rolling
its own ad-hoc fake. A single configurable fake keeps the protocol surface in
one place so that protocol drift (a new required method/property on
KanpanDataSource) is caught here rather than diverging across test files.
"""

from collections.abc import Mapping
from collections.abc import Sequence

from pydantic import TypeAdapter

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.primitives import AgentName
from imbue.mngr_kanpan.data_source import FieldValue
from imbue.mngr_kanpan.data_source import KanpanDataSource


class FakeDataSource:
    """A configurable fake KanpanDataSource for tests.

    ``compute`` either returns the configured ``result``/``errors`` pair or, if
    ``raises`` is set, raises it -- letting tests exercise both the success and
    crash paths of ``_run_data_sources_parallel`` without bespoke classes.
    """

    def __init__(
        self,
        name: str,
        result: Mapping[AgentName, dict[str, FieldValue]] | None = None,
        errors: Sequence[str] = (),
        is_remote: bool = False,
        columns: Mapping[str, str] | None = None,
        field_types: Mapping[str, TypeAdapter[FieldValue]] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._name = name
        self._result: dict[AgentName, dict[str, FieldValue]] = dict(result or {})
        self._errors: tuple[str, ...] = tuple(errors)
        self._is_remote = is_remote
        self._columns: dict[str, str] = dict(columns or {})
        self._field_types: dict[str, TypeAdapter[FieldValue]] = dict(field_types or {})
        self._raises = raises

    @property
    def name(self) -> str:
        return self._name

    @property
    def is_remote(self) -> bool:
        return self._is_remote

    @property
    def columns(self) -> dict[str, str]:
        return self._columns

    @property
    def field_types(self) -> dict[str, TypeAdapter[FieldValue]]:
        return self._field_types

    def compute(
        self,
        agents: tuple[AgentDetails, ...],
        cached_fields: dict[AgentName, dict[str, FieldValue]],
        mngr_ctx: MngrContext,
    ) -> tuple[dict[AgentName, dict[str, FieldValue]], Sequence[str]]:
        if self._raises is not None:
            raise self._raises
        return self._result, self._errors


def make_fake_data_source(
    name: str,
    result: Mapping[AgentName, dict[str, FieldValue]] | None = None,
    errors: Sequence[str] = (),
    is_remote: bool = False,
    columns: Mapping[str, str] | None = None,
    field_types: Mapping[str, TypeAdapter[FieldValue]] | None = None,
    raises: Exception | None = None,
) -> KanpanDataSource:
    """Build a FakeDataSource and assert it conforms to the KanpanDataSource protocol.

    The isinstance check (KanpanDataSource is runtime_checkable) fails the
    calling test immediately if the fake stops conforming to the protocol.
    """
    fake = FakeDataSource(
        name,
        result=result,
        errors=errors,
        is_remote=is_remote,
        columns=columns,
        field_types=field_types,
        raises=raises,
    )
    assert isinstance(fake, KanpanDataSource)
    return fake
