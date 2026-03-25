# Auto-accept rules

The following categories of fixes should always be accepted without manual review:

- Adding `Final` to constants
- Adding or removing `@pure` when the explanation is correct
- Changing function input types to be immutable, or making function output types more precise:
    - Changing `list[T]` to `Sequence[T]`
    - Changing `dict[K, V]` to `Mapping[K, V]`
    - Changing `set[T]` to `AbstractSet[T]`
    - Replacing `Any` with an actual type
- Removing inline imports, as long as it's not done via hack (e.g. certainly do NOT auto-accept replacing an inline import with a call to importlib)
- Adding `from e` to bare raises inside except blocks
- Removing module-level docstrings
- Removing Args/Returns sections from docstrings
- Removing code from `__init__.py` (moving it to the appropriate module)
- Pydantic model improvements:
    - Converting dataclasses or namedtuples to `FrozenModel`
    - Converting raw dicts with static keys to `FrozenModel`
    - Removing `__init__` methods from non-Exception classes and replacing with `Field` declarations (but NOT replacing `__init__` with something else like `__new__`)
    - Adding `Field(description=...)` to model attributes
    - Replacing `model_copy(update={"field": value})` with `model_copy_update(to_update(...))`
    - Adding `frozen=True` to config/dependency fields on interface and implementation classes
