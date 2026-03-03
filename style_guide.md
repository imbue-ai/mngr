# Summary

High level summary of our style:

    **Write python code in a stateless, functional, immutable style. Ensure that it is type safe, high quality, well-documented, and conforms to existing standards**

Each aspect of python software engineering is covered below in more detail in the corresponding section

# Style consistency

Always follow these style directives. Keep the style consistent throughout the codebase

Note that individual projects may have their own style_guide.md files--if they do, those are to be treated as *deltas* to this style guide (any directives there override the rules here) 

# Primitives

Avoid using primitives directly

Create classes that inherit from the builtin primitives

```python
from imbue.imbue_common.primitives import Probability

# Probability is a float constrained to [0.0, 1.0]
# Raises InvalidProbabilityError if out of range
chance_of_rain = Probability(0.75)
```

The point is to create *meaningful* data types such that when a reader looks at the type signature of a function or object, it is obvious what type of data is being passed in, and that data should be guaranteed to be of the correct form

If creating a primitive class, put it in `primitives.py`

If `primitives.py` becomes too large (> 500 lines), split it out into a `primitives` module with separate files for different categories of primitives (ex: `ids.py`, `strings.py`, `times.py`, etc.)

## IDs

Always create a specific id class for each type of object that has an id by inheriting from the `RandomId` class in the `imbue_common` library

```python
from imbue.imbue_common.ids import RandomId


class TodoId(RandomId):
    """Unique identifier for a todo item."""

    ...
```

## File Paths

Always use `pathlib.Path` instead of `str` for file paths and directory paths

```python
from pathlib import Path

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel


class TodoStorageConfiguration(FrozenModel):
    """Configuration for todo storage."""

    storage_directory: Path = Field(description="Directory where todo files are stored")
    backup_directory: Path = Field(description="Directory for backup files")


def read_todo_file(file_path: Path) -> str:
    """Read a todo file and return its contents."""
    return file_path.read_text()


def write_todo_file(file_path: Path, content: str) -> None:
    """Write content to a todo file."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content)
```

Using `Path` instead of `str` provides better type safety, clearer intent, and makes path operations more explicit and less error-prone

## Strings

Always use the pydantic classes for fields like AnyUrl and IPv4Address

```python
from pydantic import AnyUrl
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel


class TodoSyncConfiguration(FrozenModel):
    """Configuration for syncing todos with a remote server."""

    sync_server_url: AnyUrl = Field(description="The URL of the sync server")
    webhook_callback_url: AnyUrl = Field(description="URL to receive sync notifications")
```

Create domain-specific string sub-classes that inherit from classes like `NonEmptyStr` (from `imbue_common`) to ensure that data types are expressed sensibly.

```python
from imbue.imbue_common.primitives import NonEmptyStr


class TodoDescription(str):
    """A description for a todo item (may be empty)."""

    ...


class UserName(NonEmptyStr):
    """The name of a user."""

    ...
```

## Numbers

Create domain-specific numeric sub-classes that inherit from classes like `PositiveInt`, `NonNegativeInt`, `PositiveFloat`, or `NonNegativeFloat` (from `imbue_common`) to ensure that numeric data types are constrained appropriately.

```python
from imbue.imbue_common.primitives import NonNegativeInt
from imbue.imbue_common.primitives import PositiveFloat
from imbue.imbue_common.primitives import PositiveInt


class TodoCount(NonNegativeInt):
    """A count of todo items. Must be >= 0."""

    ...


class RetryCount(NonNegativeInt):
    """The number of retry attempts. Must be >= 0."""

    ...


class MaxTodosPerList(PositiveInt):
    """The maximum number of todos allowed in a list. Must be > 0."""

    ...


class TaskDurationHours(PositiveFloat):
    """The estimated duration of a task in hours. Must be > 0."""

    ...
```

## Secrets

Always use SecretStr for any secret data

```python
from pydantic import SecretStr


class TodoSyncCredentials(FrozenModel):
    """Credentials for authenticating with the sync server."""

    username: UserName = Field(description="Sync server username")
    api_key: SecretStr = Field(description="API key for authentication")
    encryption_key: SecretStr = Field(
        description="Key used to encrypt todo data in transit"
    )
```

Unless otherwise stated in the project, assume that secret data (ex: API keys, tokens, passwords, etc.) will be accessible via `os.environ`

## Times

Always use timezone aware, UTC-anchored datetime object for times

```python
from datetime import datetime
from datetime import timezone

from imbue.imbue_common.pure import pure


@pure
def get_current_utc_timestamp() -> datetime:
    return datetime.now(timezone.utc)
```

## Currency

Always use Decimal to express monetary values

```python
from decimal import Decimal
from functools import cached_property

from pydantic import computed_field


class TodoCostEstimate(FrozenModel):
    """Describes how much a todo item may cost to accomplish."""

    todo_id: TodoId = Field(description="The todo this estimate belongs to")
    estimated_cost_dollars: Decimal = Field(description="Estimated cost (in dollars)")
```

# Enums

All enums should inherit from `UpperCaseStrEnum`

All keys for enums should be fully UPPER_CASE

All values for enums should be `auto()`

```python
from enum import auto

from imbue.imbue_common.enums import UpperCaseStrEnum


class TodoStatus(UpperCaseStrEnum):
    """The completion status of a todo item."""

    PENDING = auto()
    IN_PROGRESS = auto()
    COMPLETED = auto()


class TodoPriority(UpperCaseStrEnum):
    """The priority level of a todo item."""

    LOW = auto()
    MEDIUM = auto()
    HIGH = auto()
```

All values for enums will thus end up as a string that is equal to the key name, eg, "UPPER_CASE_KEY_NAME"

# Exhaustive Pattern Matching

When branching on a finite set of cases, all possibilities must be handled explicitly with no implicit defaults.

**For enums, always use match statements.** If something can be matched, prefer to use match. The type checker can verify exhaustiveness at compile time.

**For complex conditions where match doesn't make sense,** use if/elif/else chains. All if/elif chains must end with an else clause. If the else case should never happen, raise an exception.

## Match statements with assert_never (for enums and matchable values)

Always use match statements when branching on enum values. Use `assert_never` from typing to enable the type checker to catch missing cases.

```python
from typing import assert_never


class OutputFormat(UpperCaseStrEnum):
    """The format for outputting todo data."""

    JSON = auto()
    CSV = auto()
    YAML = auto()


@pure
def serialize_todo_list(
    todo_list: TodoList,
    output_format: OutputFormat,
) -> str:
    """Serialize a todo list to the specified format."""
    match output_format:
        case OutputFormat.JSON:
            return json.dumps(todo_list.model_dump())
        case OutputFormat.CSV:
            return convert_todo_list_to_csv(todo_list)
        case OutputFormat.YAML:
            return convert_todo_list_to_yaml(todo_list)
        case _ as unreachable:
            assert_never(unreachable)
```

If a new enum value is added (e.g., `XML = auto()`), the type checker will report an error at every location where the enum is matched, forcing the developer to handle the new case.

## If/elif/else with mandatory else clause (for complex conditions)

When you need complex conditional logic that cannot be expressed with match statements, use if/elif/else chains. The else clause is mandatory. If the else case should never happen, raise an exception. It is acceptable for branches to contain only `pass`.

```python
from imbue.imbue_common.errors import SwitchError


@pure
def categorize_todo_by_priority_and_age(
    todo: TodoItem,
    current_time: datetime,
) -> str:
    """Categorize a todo based on multiple conditions."""
    age_days = (current_time - todo.created_at).days

    if todo.priority == TodoPriority.HIGH and age_days > 7:
        return "urgent_overdue"
    elif todo.priority == TodoPriority.HIGH and age_days <= 7:
        return "urgent_recent"
    elif todo.priority == TodoPriority.MEDIUM and todo.is_completed:
        pass
    elif todo.priority == TodoPriority.LOW and age_days > 30:
        return "stale"
    else:
        raise SwitchError(
            f"Unhandled categorization case: priority={todo.priority}, "
            f"age_days={age_days}, is_completed={todo.is_completed}"
        )
```

Never write if/elif chains without a final else clause. The else clause ensures all cases are handled and makes it explicit when an unexpected condition occurs.

Prefer to use match statements when matching against enums or other finite sets of values.

# Validation

All validation should be done purely through pydantic and types, not with ad-hoc code

```python
from typing import Any
from typing import Self

from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema
from pydantic_core import core_schema


class TodoTitle(str):
    """A non-empty title for a todo item with length constraints."""

    def __new__(cls, value: str) -> Self:
        if not value or not value.strip():
            raise ValueError("Todo title cannot be empty")
        if len(value) > 200:
            raise ValueError("Todo title cannot exceed 200 characters")
        return super().__new__(cls, value.strip())

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: Any,
        handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls,
            core_schema.str_schema(min_length=1, max_length=200),
        )
```

Use `Annotated` with pydantic constraints for simple numeric validation when a domain-specific type is not warranted

```python
from typing import Annotated

from pydantic import Field

from imbue.imbue_common.primitives import PositiveInt


class TodoAppConfiguration(FrozenModel):
    """Configuration settings for the todo application."""

    max_todos_per_list: PositiveInt = Field(description="Maximum todos per list")
    max_title_length: Annotated[int, Field(gt=0, le=500)] = Field(
        description="Maximum length for todo titles"
    )
```

Prefer creating validated primitive types (like `TodoTitle` or domain-specific numeric types) over repeating validation logic

Never validate data using ad-hoc code in functions--always use pydantic models or validated primitive types

# Errors

## Exception hierarchy

All raised Exceptions should inherit from a base class that is specific to that library or app.

Never raise built-in Exceptions directly (except NotImplementedError). Instead, create a new type that inherits from both the base error class for the package and the built-in. Avoid creating and raising such exceptions unless it very obviously applies (ex: this is clearly a timeout error)

```python
class TodoAppError(Exception):
    """Base exception for all todo application errors."""

    ...


class TodoNotFoundError(TodoAppError, KeyError):
    """Raised when a todo item cannot be found."""

    def __init__(self, todo_id: TodoId) -> None:
        self.todo_id = todo_id
        super().__init__(f"Todo with ID '{todo_id}' not found")


class TodoAlreadyCompletedError(TodoAppError, ValueError):
    """Raised when attempting to complete an already completed todo."""

    def __init__(self, todo_id: TodoId) -> None:
        self.todo_id = todo_id
        super().__init__(f"Todo with ID '{todo_id}' is already completed")


class StorageInitializationError(TodoAppError, OSError):
    """Raised when storage cannot be initialized."""

    ...
```

When catching exceptions from external libraries or built-in types, always wrap them with our own error types using `raise ... from e` to preserve the exception chain

```python
def load_todos_from_json_file(file_path: Path) -> tuple[TodoItem, ...]:
    """Raises TodoStorageError if the file cannot be read or parsed."""
    try:
        raw_data = file_path.read_text()
    except OSError as e:
        raise TodoStorageError(f"Cannot read file: {file_path}") from e
    try:
        parsed_data = json.loads(raw_data)
    except json.JSONDecodeError as e:
        raise TodoStorageError(f"Invalid JSON in file: {file_path}") from e
    return tuple(TodoItem.model_validate(item) for item in parsed_data)
```

**IMPORTANT**: Within an `except` clause, always use `raise ... from err` or `raise ... from None` when raising exceptions. Never use a bare `raise SomeException()` inside an except block.

- Use `raise ... from e` to preserve the exception chain when the original exception is relevant
- Use `raise ... from None` to suppress the original exception when it's not relevant to the caller

```python
def example_exception_handling_with_chaining() -> None:
    """Examples of proper exception chaining."""
    # Use "from e" to preserve the exception chain
    try:
        result = dangerous_operation()
    except ValueError as e:
        raise TodoError("Operation failed") from e

    # Use "from None" to suppress irrelevant implementation details
    try:
        value = SomeEnum(user_input)
    except ValueError:
        raise TodoError(f"Invalid value: {user_input}") from None
```

Never use a blanket `except:` clause! Always catch the narrowest specific exception type that can be caught at a given point.

Always log errors that are caught (at the appropriate level--trace or debug if this is expected, or warning if this is from us trying to make the code more robust and there's no other choice, error only if this is a more general top level error handler)

## Try/except

Each try/except blocks should only span a single statement, and should catch precisely the errors that we want to handle from that statement.

```python
from pathlib import Path

from imbue.imbue_common.primitives import InvalidProbabilityError
from imbue.imbue_common.primitives import Probability


class TodoCostLedgerEntry(FrozenModel):
    """An entry in the expected cost ledger for todos."""

    todo_cost_estimate: TodoCostEstimate = Field(
        description="The cost estimate associated with this ledger entry"
    )
    execution_probability: Probability = Field(
        description="The probability that this cost will be incurred"
    )


class CostLedgerImportResult(FrozenModel):
    """Result of importing a cost ledger from CSV."""

    valid_entries: tuple[TodoCostLedgerEntry, ...] = Field(description="Successfully parsed entries")
    invalid_todo_ids: tuple[TodoId, ...] = Field(description="IDs that failed validation")


def import_cost_ledger_from_csv(
    csv_path: Path,
) -> CostLedgerImportResult:
    invalid_todo_ids: list[TodoId] = []
    ledger_entries: list[TodoCostLedgerEntry] = []
    for line in csv_path.read_text().splitlines():
        todo_id_str, cost_str, chance_of_execution = line.split(",")
        todo_id = TodoId(todo_id_str)
        cost = Decimal(cost_str)
        try:
            execution_probability = Probability(float(chance_of_execution))
        except InvalidProbabilityError:
            invalid_todo_ids.append(todo_id)
            continue
        ledger_entry = TodoCostLedgerEntry(
            todo_cost_estimate=TodoCostEstimate(
                todo_id=todo_id,
                estimated_cost_dollars=cost,
            ),
            execution_probability=execution_probability,
        )
        ledger_entries.append(ledger_entry)

    return CostLedgerImportResult(
        valid_entries=tuple(ledger_entries),
        invalid_todo_ids=tuple(invalid_todo_ids),
    )
```

Always use `tenacity` for retrying specific types of errors when necessary. Keep the retry logic as high in the call chain as possible to avoid scattering lots of ad-hoc retries all over the program.

```python
from tenacity import retry
from tenacity import retry_if_exception_type
from tenacity import stop_after_attempt
from tenacity import wait_exponential


class TodoNotificationError(TodoAppError):
    """Raised when notification delivery fails."""

    ...


class TodoNotificationService(MutableModel):
    """Service for sending todo reminder notifications."""

    @retry(
        retry=retry_if_exception_type(TimeoutError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
    )
    def send_reminder(self, reminder: TodoReminder) -> None:
        """Raises TodoNotificationError if notification cannot be sent after retries."""
        try:
            self._send_notification(reminder)
        except ConnectionError as e:
            raise TodoNotificationError(
                f"Cannot send reminder: {reminder.reminder_id}"
            ) from e
```

Be very conservative with what exceptions are caught. Prefer to crash instead of catching errors.

## Timeouts

When calling external commands or making network requests, always use a two-threshold timeout pattern:

1. **Hard timeout**: Set a generous timeout that represents "this is definitely broken" (e.g. 15s for filesystem ops, 60s for network calls, 300s for installations). This prevents infinite hangs.
2. **Warning threshold**: After the command completes successfully, check if it took longer than a "suspicious" duration (e.g. 2s for filesystem ops, 15s for network calls). If so, emit a warning so we notice things becoming slow *before* they become totally broken.

This pattern allows us to notice degradation and diagnose slowdowns before they become outright failures.

# Docstrings

We want our code to be self-documenting as much as possible

Only create docstrings for functions and methods that require explanation beyond what can be inferred from the function name, parameter names, and parameter comments

Never create docstrings for methods that already have predefined meanings (ex: `__init__`, `__new__`, `__str__`, etc.).

Always create short, useful docstrings for `@abstractmethod` methods. Since abstract methods have no implementation, the docstring is the only place to communicate what the method should do. Keep these docstrings concise (one sentence) and focused on the contract the implementation must fulfill.

If a docstring is required, it should be short (just a line or two) and concisely describe *what* the function does

Never include Args or Returns sections in docstrings

If a parameter needs explanation, include a `#` comment on the line immediately before that parameter. However, it is better to make the function name and parameter names longer and clearer so that no comment is needed

Similarly, a `#` comment can precede the return type annotation if necessary, but again, prefer clear naming (and creating structured, well-named return types) over comments

Never put type information inside a docstring or comment--it is duplicative with the function signature

```python
# a function that is so simple that no docstring or comments are needed
@pure
def filter_todos_by_status(
    todos: tuple[TodoItem, ...],
    status: TodoStatus,
) -> tuple[TodoItem, ...]:
    return tuple(t for t in todos if t.status == status)


# An example that is complex enough to warrant a docstring and some comments
@pure
def walk_todo_tree(
    root_todo: TodoItem,
    # A callback function to invoke on each todo item as it is visited. If it returns a non-None result, 
    # the original todo item will be replaced with the returned item in the tree.
    visit_callback: Callable[[TodoItem], TodoItem | None],
# the (possibly new) root created by the traversal
) -> TodoItem:
    """Traverse the todo tree in depth-first order, invoking a callback on each todo item."""
    ...
```

Always create docstrings for classes. The docstring for a class should contain *only* a 1-2 sentence description of the high level purpose of the class

Never include the attributes of a class in the class docstring

Never create docstrings for modules (unless they are completely standalone scripts).

# Comments

Comments should be used to describe the next "block" of code.  This might be at the module level, or the function level

Keep comments focused on what is happening at a higher level than the code. Avoid using variable names in comments.  Describe what is about to happen next

```python
class ArchiveCompletedTodosResult(FrozenModel):
    """Result of archiving completed todos from a list."""

    updated_list: TodoList = Field(description="The list with archived items removed")
    archived_todos: tuple[TodoItem, ...] = Field(description="Todos that were archived")


@pure
def archive_todos_completed_before(
    todo_list: TodoList,
    archive_before_date: datetime,
) -> ArchiveCompletedTodosResult:
    
    # Separate todos into those to keep and those to archive
    todos_to_keep: list[TodoItem] = []
    todos_to_archive: list[TodoItem] = []
    for todo in todo_list.todos:
        if todo.status == TodoStatus.COMPLETED and todo.completed_at < archive_before_date:
            todos_to_archive.append(todo)
        else:
            todos_to_keep.append(todo)

    # Build the result
    updated_list = todo_list.model_copy_update(
        to_update(todo_list.field_ref().todos, tuple(todos_to_keep)),
    )
    return ArchiveCompletedTodosResult(
        updated_list=updated_list,
        archived_todos=tuple(todos_to_archive),
    )
```

# Naming

Always use very literal, concrete names (ex: `find_overdue_incomplete_todos`)

If necessary, prefer longer names (ex: `filter_todos_by_priority_and_status_within_date_range`)

Never use single-letter variable names *except* for the conventional cases (ex: `for i in range(10):`)

Only use the very most common abbreviations (ex: `max`, `min`, `idx`, `temp`, etc.). Otherwise spell the word out fully (ex: `approximate` instead of `approx`)

Private function, class, and variable names (those that are not imported anywhere else) should be prefixed with `_`

```python
from typing import Final


_DEFAULT_TODO_PAGE_SIZE: Final[int] = 50


@pure
def _sort_todos_by_due_date_ascending(
    todos: tuple[TodoItem, ...],
) -> tuple[TodoItem, ...]:
    return tuple(sorted(todos, key=lambda todo: todo.due_date or datetime.max))


class _TodoTitleNormalizer:
    """Internal helper for normalizing and cleaning todo titles."""

    ...
```

Public function, class, and variable names should be globally unique (within the project)

Avoid abbreviations (except for the very most common like "max" or "len")

Always prefix *internal boolean variables* with `is_`. Variables that are part of 3rd-party libraries, or which are user-facing configuration (eg, settings or CLI args) do *not* need to follow that convention, but all *internal* variables should (eg, when we convert from the settings to our internal representation, we should convert the names)

```python
class TodoFilter(FrozenModel):
    """Options for filtering a todo list query."""

    is_completed_only: bool = Field(description="Only show completed todos")
    is_high_priority_only: bool = Field(description="Only show high priority todos")
    is_overdue_only: bool = Field(description="Only show overdue todos")
```

Always use `value_by_key` for dictionaries and mappings

```python
class TodoListCollection(FrozenModel):
    """Manages multiple todo lists for a user."""

    todo_list_by_list_id: dict[TodoListId, TodoList] = Field(
        description="All todo lists indexed by their unique identifier"
    )
```

Avoid using `num`. Prefer `count` or `idx` instead

```python
class TodoListStatistics(FrozenModel):
    """Tracks statistics about todos in a list."""

    total_todo_count: int = Field(description="Total number of todos in the list")
    completed_todo_count: int = Field(description="Number of completed todos")
    overdue_todo_count: int = Field(description="Number of overdue todos")


def get_todo_at_display_idx(self, display_idx: int) -> TodoItem:
    ...
```

# Type hinting

Always include complete type hints

Avoid importing from `typing` unless strictly necessary

```python
from typing import Final


MAX_TODOS_PER_LIST: Final[int] = 1000


@pure
def find_todos_matching_title_or_description(
    todo_list: TodoList,
    search_text: str,
    max_result_count: int,
) -> list[TodoItem]:
    ...
```

Never use `dict` unless the keys are truly dynamic--if the properties come from a fixed set that is known ahead of time, create an object instead

## Immutable input types

Function *inputs* should be typed using immutable abstract types rather than mutable concrete types. This allows the type checker to catch accidental mutations of input data, which is almost always a mistake.

Use these immutable types for parameters:
- `Sequence[T]` instead of `list[T]`
- `Mapping[K, V]` instead of `dict[K, V]`
- `AbstractSet[T]` instead of `set[T]`

```python
from collections.abc import Mapping
from collections.abc import Sequence


@pure
def find_todos_with_any_tag(
    todos: Sequence[TodoItem],
    allowed_tags: Mapping[str, TagPriority],
) -> tuple[TodoItem, ...]:
    return tuple(
        todo for todo in todos
        if any(tag in allowed_tags for tag in todo.tags)
    )
```

Return types should use concrete mutable types like `list` or `dict`, since the caller owns the returned value and may need to modify it. Use `list` for sequences and `dict` for mappings in return types.

If something needs to be changed, return an updated copy instead of mutating the input.

# State

Use an immutable, functional approach.  Accumulate all changes rather than updating any data "in-place"

Avoid mutating objects created outside the function (unless they are "Implementations", see below).  Instead, prefer to create an updated copy whenever possible.

## Type-safe model_copy_update

When creating updated copies of frozen or mutable models, always use the type-safe `model_copy_update`/`to_update`/`field_ref` pattern instead of passing raw string dictionaries to `model_copy(update=...)`. This ensures that field names are checked by the type system and refactoring tools can find all usages of a field.

```python
from imbue.imbue_common.model_update import to_update


@pure
def add_tag_to_todo(todo_item: TodoItem, tag_to_add: Tag) -> TodoItem:
    updated_tags = todo_item.tags + (tag_to_add,)
    return todo_item.model_copy_update(
        to_update(todo_item.field_ref().tags, updated_tags),
    )
```

- `field_ref()` returns a proxy that records attribute access, making field references type-safe
- `to_update(field_ref, value)` creates a type-checked `(field_name, value)` pair
- `model_copy_update(...)` accepts `to_update()` pairs and creates an updated copy of the model
- Multiple fields can be updated at once by passing multiple `to_update()` calls to `model_copy_update()`

Never pass raw string dictionaries like `model_copy(update={"field_name": value})` -- always use the type-safe pattern above.

Never call `model_copy(update=to_update_dict(...))` directly -- always use `model_copy_update(...)` instead.

Never re-assign to the same function-scoped variable. Instead, create a new variable with an updated name

```python
class ValidatedTodoInput(FrozenModel):
    """Validated user input for creating a todo. Validation happens in the type."""

    title: TodoTitle = Field(description="Validated todo title")
    description: str = Field(description="Todo description")


@pure
def create_todo_from_validated_input(validated_input: ValidatedTodoInput) -> TodoItem:
    new_todo_id = TodoId.generate()
    return TodoItem(
        todo_id=new_todo_id,
        title=validated_input.title,
        description=validated_input.description,
        status=TodoStatus.PENDING,
        priority=TodoPriority.MEDIUM,
        due_date=None,
        completed_at=None,
        is_completed=False,
        is_archived=False,
        is_pinned=False,
        tags=(),
    )
```

Prefer to use ternary expressions instead of mutating variables for this reason (ex: "`value = new_value if condition else old_value`" instead of assigning to `value` multiple times)

Avoid using the `global` keyword.  Instead, pass all state explicitly through from the top level of the program

```python
def main() -> None:
    configuration = load_todo_app_configuration()
    todo_repository = create_todo_repository(configuration.storage_settings)
    notification_service = create_notification_service(configuration.notification_settings)

    run_todo_app(
        configuration=configuration,
        todo_repository=todo_repository,
        notification_service=notification_service,
    )


@pure
def add_todo_to_list(todo_item: TodoItem, todo_list: TodoList) -> TodoList:
    return todo_list.with_added_todo(todo_item)
```

# Classes

In our functional programs, there are only 3 types of classes:

1. Frozen objects: these classes represent domain objects--the data that matters for the program.  These are generally the "nouns" of the program
2. Interfaces: these classes control how all "state" is mutated--both the state inside the program, and the state of the external world. Interface classes are simply collections of abstract methods (with types and docstrings)
3. Implementations: these classes inherit from interface classes in order to implement any manipulation of state (either their own private state attributes, or external world state)

It is easy to tell what any given class is by which class it inherits from:

1. Frozen objects inherit from `FrozenModel` (which inherits from `BaseModel` in `pydantic`)
2. Interfaces inherit from `ABC` (from the `abc` module) and from `MutableModel` class (which inherits from `BaseModel` in `pydantic`). This is done because it is useful to have attributes on interfaces sometimes without having to overcomplicate the interfaces)
3. Implementations inherit from the relevant interface class. 

## Frozen objects

Use frozen objects to express all data in a program

Frozen objects must inherit from `FrozenModel`. Never store application data in raw python dictionaries or mappings. Dictionaries should only be used if the keys *must* be dynamic (ex: a run-time mapping from some key that happens to be a string)

Frozen objects must have attributes listed immediately after their docstring as `variable_name: TypeName`. Attributes should have their description captured in the `description` attribute of the pydantic `Field` object (so it shows up in the schema)

```python
class ReminderId(RandomId):
    """Unique identifier for a reminder."""

    ...


class TodoReminder(FrozenModel):
    """A scheduled reminder for a todo item."""

    reminder_id: ReminderId = Field(description="Unique identifier")
    todo_id: TodoId = Field(description="The todo this reminder is for")
    remind_at: datetime = Field(description="When to trigger the reminder")
    is_sent: bool = Field(default=False, description="Whether sent")

    def with_marked_as_sent(self) -> "TodoReminder":
        return self.model_copy_update(
            to_update(self.field_ref().is_sent, True),
        )
```

Frozen objects should use computed fields and the correct caching decorator to cache read-only derived properties

```python
from functools import cached_property

from pydantic import computed_field


class TodoBatch(FrozenModel):
    """A batch of todos for bulk processing."""

    batch_id: RandomId = Field(description="Unique identifier for this batch")
    todos: tuple[TodoItem, ...] = Field(description="Todos in this batch")

    @computed_field
    @cached_property
    def high_priority_count(self) -> int:
        return sum(1 for t in self.todos if t.priority == TodoPriority.HIGH)
```

Frozen objects should avoid making computed or derived fields which require importing anything else from the program. Instead, such attributes should be computed using functions in some other module (helps prevent circular imports)

Frozen objects should be contained in a file named `data_types.py` at the root of the package (helps avoid circular imports). If the file gets too large (> 500 lines), it can be converted to a `data_types` module instead

## Interface classes

Use interface classes to capture all stateful interactions in the program

Avoid putting any "pure" functions on interface classes (prefer to make top-level functions instead)

Never use `Protocol`'s' or "duck typing" for interface classes

Always make explicit interface classes from which all implementations inherit

Always have interface classes inherit from both `MutableModel` and the `ABC` class in the `abc` module

Interface classes should declare their attributes using pydantic `Field` declarations (just like frozen objects), and use `@abstractmethod` to annotate their methods.

Mark fields as `frozen=True` individually when they should not be modified after construction. This provides immutability for configuration-like fields while still allowing the class to have mutable state where needed.

```python
from abc import ABC
from abc import abstractmethod

from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel


class TodoRepositoryInterface(MutableModel, ABC):
    """Defines the contract for persisting and retrieving todo data."""

    storage_path: Path = Field(frozen=True, description="Directory where todo files are stored")
    todo_count: int = Field(description="Number of todos in storage")

    @abstractmethod
    def save_todo(self, todo_item: TodoItem) -> None:
        """Persist a todo item to storage, overwriting if it already exists."""

    @abstractmethod
    def get_todo_by_id(self, todo_id: TodoId) -> TodoItem | None:
        """Retrieve a todo by its ID, returning None if not found."""

    @abstractmethod
    def delete_todo(self, todo_id: TodoId) -> None:
        """Remove a todo from storage. Raises TodoNotFoundError if not found."""
```

When possible, create "paired" function names in interface classes (ex: if there is a `start` method, there should be a `stop` method). Use words that are natural opposites (ex: `start` and `stop` instead of `start` and `shutdown`)

```python
from abc import ABC
from abc import abstractmethod


class TodoChange(FrozenModel):
    """Represents a change to sync between client and server."""

    change_id: RandomId = Field(description="Unique identifier for this change")
    todo_id: TodoId = Field(description="The todo that was changed")
    change_type: str = Field(description="Type of change: create, update, delete")


class TodoSyncServiceInterface(MutableModel, ABC):
    """Defines the contract for synchronizing todos with a remote server."""
    
    @abstractmethod
    def connect(self, server_url: str) -> None:
        """Establish a connection to the sync server."""

    @abstractmethod
    def disconnect(self) -> None:
        """Close the connection to the sync server."""

    @abstractmethod
    def push_changes(self, changes: tuple[TodoChange, ...]) -> None:
        """Upload local changes to the remote server."""

    @abstractmethod
    def pull_changes(self) -> tuple[TodoChange, ...]:
        """Download pending changes from the remote server."""
```

If you are creating an interface class, create it in a file named `interfaces.py` at the root of the package (helps avoid circular imports)

If `interfaces.py` gets too large (> 500 lines), it can be converted to an `interfaces` module instead

## Implementation classes

Only use implementation classes to manipulate state--all other logic should be contained in pure functions that interact with data classes directly

All state manipulation (eg all modification of variables at runtime, all filesystem and network access, etc) should happen in an implementation class

Implementation classes must inherit from the relevant interface class(es).

As with interface classes, mark fields as `frozen=True` when they should not be modified after construction (typically configuration or dependency fields). Only leave fields unfrozen if they represent mutable state that will be updated during the object's lifetime.

```python
from pathlib import Path

from pydantic import Field


class FileTodoRepository(TodoRepositoryInterface):
    """File-based implementation of the todo repository."""

    storage_directory: Path = Field(frozen=True, description="Directory where todo files are stored")
    is_initialized: bool = Field(default=False, description="Whether initialized")

    def initialize(self) -> None:
        """Raises StorageInitializationError if the directory cannot be created."""
        try:
            self.storage_directory.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise StorageInitializationError(
                f"Cannot create storage directory: {self.storage_directory}"
            ) from e
        self.is_initialized = True

    def shutdown(self) -> None:
        self.is_initialized = False
```

Implementation classes are the *only* place where assignments / object mutations are allowed (ex: `self.foo = something`)

```python
class InMemoryTodoRepository(TodoRepositoryInterface):
    """In-memory implementation of the todo repository for testing."""

    todo_by_id: dict[TodoId, TodoItem] = Field(
        default_factory=dict,
        description="All stored todos indexed by ID",
    )
    save_count: int = Field(default=0, description="Number of save operations")

    def save_todo(self, todo_item: TodoItem) -> None:
        self.todo_by_id[todo_item.todo_id] = todo_item
        self.save_count = self.save_count + 1

    def delete_todo(self, todo_id: TodoId) -> None:
        del self.todo_by_id[todo_id]
```

There will often be multiple different implementation classes for a single interface. This is particularly helpful for testing (since a simplified local implementation can be used instead of one that eg accesses remote resources)

Implementation classes should be contained within their own named module off of the root of the package (ex: if the package is `foobar` and the interface being implemented is `DatabaseInterface`, then the implementations should go in `foobar.database`)

# Functions and methods

All functions and methods should have a single, clear purpose. Because of this, the names of functions and methods should be very precise and self-documenting. It is acceptable for the names to become fairly long. It should be very obvious what a function or method does just from reading its name

Functions and methods should be relatively short (10-50 lines)

Functions and methods that are long should be written in "blocks", where each block is prefixed with a comment explaining that block of code

```python
class TodoSummaryReport(FrozenModel):
    """A summary report of todo list status."""

    list_name: TodoListName = Field(description="Name of the list")
    report_date: datetime = Field(description="Date of the report")
    statistics: TodoStatistics = Field(description="Summary statistics")


@pure
def generate_todo_summary_report(
    todo_list: TodoList,
    report_date: datetime,
) -> TodoSummaryReport:
    # Count todos by status
    completed_todos = tuple(t for t in todo_list.todos if t.is_completed)
    pending_todos = tuple(t for t in todo_list.todos if not t.is_completed)

    # Identify overdue items
    overdue_todos = tuple(
        t for t in pending_todos
        if t.due_date is not None and t.due_date < report_date
    )

    # Build the summary statistics
    statistics = TodoStatistics(
        total_count=len(todo_list.todos),
        completed_count=len(completed_todos),
        overdue_count=len(overdue_todos),
    )

    # Create the final report
    return TodoSummaryReport(
        list_name=todo_list.name,
        report_date=report_date,
        statistics=statistics,
    )
```

Avoid using default arguments in function and method signatures. Require callers to explicitly pass all parameters. By convention, prefer to call functions by naming all parameters explicitly at the call site

## Functions

Almost all functions should be pure (have no side-effects)

All pure functions should be marked with the `@pure` decorator from `imbue_common`. Note that this decorator is currently advisory only and is not enforced at runtime.

```python
from imbue.imbue_common.pure import pure


PRIORITY_SORT_ORDER: Final[dict[TodoPriority, int]] = {
    TodoPriority.HIGH: 0,
    TodoPriority.MEDIUM: 1,
    TodoPriority.LOW: 2,
}


@pure
def sort_todos_by_priority_then_due_date(
    todos: tuple[TodoItem, ...],
) -> tuple[TodoItem, ...]:
    return tuple(
        sorted(
            todos,
            key=lambda t: (PRIORITY_SORT_ORDER[t.priority], t.due_date or datetime.max),
        )
    )
```

Never write code outside a function (except for the call to `main()`, the declaration of constants, and imports.) Instead, encapsulate all logic inside a function

```python
from typing import Final


DEFAULT_PAGE_SIZE: Final[int] = 25


@pure
def filter_todos_by_completion_status(
    todos: tuple[TodoItem, ...],
    is_completed: bool,
) -> tuple[TodoItem, ...]:
    return tuple(t for t in todos if t.is_completed == is_completed)


def main() -> None:
    configuration = load_todo_app_configuration()
    todo_repository = create_todo_repository(configuration)
    run_todo_app(configuration, todo_repository)


if __name__ == "__main__":
    main()
```

## Methods

Frozen object methods must be logically pure (eg, have no side-effects).

However, frozen object methods *are* allowed to use caching decorators. For example, derived computed properties are encouraged everywhere when there are expensive operations

```python
class TodoArchive(FrozenModel):
    """An archive of completed todos."""

    archive_id: RandomId = Field(description="Unique identifier")
    archived_todos: tuple[TodoItem, ...] = Field(description="Archived items")

    @computed_field
    @cached_property
    def total_archived_count(self) -> int:
        return len(self.archived_todos)

    def with_added_item(self, todo_to_archive: TodoItem) -> "TodoArchive":
        return self.model_copy_update(
            to_update(self.field_ref().archived_todos, self.archived_todos + (todo_to_archive,)),
        )
```

Avoid pure methods on implementation classes. Prefer creating top level functions instead

```python
class TodoDisplayInterface(ABC, MutableModel):
    """Interface for displaying todos."""

    @abstractmethod
    def display_todo(self, todo: TodoItem) -> None:
        """Render a todo item to the output destination."""


@pure
def format_todo_for_display(todo: TodoItem, is_verbose: bool) -> str:
    status_marker = "[x]" if todo.is_completed else "[ ]"
    if is_verbose and todo.description:
        return f"{status_marker} {todo.title}\n    {todo.description}"
    return f"{status_marker} {todo.title}"


class TodoDisplayService(TodoDisplayInterface):
    """Service for displaying todos to the console."""

    is_verbose_mode: bool = Field(description="Whether to show detailed output")

    def display_todo(self, todo: TodoItem) -> None:
        formatted = format_todo_for_display(todo, self.is_verbose_mode)
        print(formatted)
```

# Constants

Always use FULL_UPPER_CASE for constants

Always declare constants to be `Final`

```python
from typing import Final

MAX_TODO_TITLE_LENGTH: Final[int] = 200
```

Never mutate a constant

Never mutate the `os.environ` of the current process

Avoid using raw primitive values inline in the code. Instead, use a constant for any hard-coded values (except for globally unique strings like environment key names--there's no need to make a special constant just for that string)

# Imports

Always use absolute imports

Never use relative imports

```python
# Always use absolute imports, never relative
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.ids import RandomId
from pydantic import Field
```

Imports will be automatically sorted into a single import per line

You may use inline imports when initially generating code, but remove them during the testing phase (and there should be test that checks for inline imports)

# Modules

Always name modules and python files using "snake_case"

Always prefix private modules (those not intended to be imported outside the current module) with "_"

Never put any code in an `__init__.py` file

Use a "layered import" style: enforce a strict ordering of imports between submodules by using the layered approach from `import-linter`

Ensure that it is configured in any given `pyproject.toml` by adding a block like this:

```toml
[[tool.importlinter.contracts]]
name = "Todo app layers contract"
type = "layers"
layers = [
    "todo_app.cli",
    "todo_app.services",
    "todo_app.repository",
    "todo_app.interfaces",
    "todo_app.data_types",
    "todo_app.primitives",
]
```

The highest level module will usually be something like "cli" or "app", and the lowest will be something like "primitives" or "constants". Modules inbetween should be at the semantic level of the application, building up from lowest level details to higher.

# Package management

Always use `uv` and `pyproject.toml` for managing dependencies and build configuration

Always use `just` to create scripts for common activities like building, testing, and deploying

# Formatting

Code is automatically formatted with `ruff`

# Logging

Always use `loguru` for logging

## Log levels

Always use the right log level for your statement:

- `logger.exception`: use this to capture all *unexpected* exceptions. After calling this, just call "raise" to continue propagating the exception (since it is unexpected)
- `logger.error`: use this for unexpected error situations (where there is no Exception, otherwise use)
- `logger.warning`: use this for things that seem suspicious, but not worth crashing over (or you are in a part of the code that should not crash). These should be purged aggressively if ever seen in a log
- `logger.info`: Use this to describe _what_ the application is doing at a high level. These messages are ideally something that would make sense to a user of the program. Info logs belong in CLI/user-facing code, not in library/API code
- `logger.debug`: Use this to describe _how_ the application is doing it. These messages ideally make sense to the developer of the program. This is the primary level for library/API code
- `logger.trace`: Use this for detailed parameter values and state. These messages are disabled by default, and will generally only be used when debugging a specific problem

## Log placement guidelines

The purpose of log statements is to tell a story to the reader about what is happening in the program. They help us understand program execution and debug issues.

**Every log statement should start with a verb** (ex: "Saving todo to repository", "Failed to send notification", etc). This makes it much easier to read, and understand what is happening / has happened.

The verbs should be past tense (eg, end with "ed") in normal log statements (which should be placed *after* the event) or active (eg "ing" form) if using `log_span` (which should be placed *before* the event).

**Use `log_span` to wrap actions.** When logging an action that is about to happen, use the `log_span` context manager instead of a bare `logger.debug`. This emits a debug message on entry and a trace message with elapsed time on exit, making it easy to see how long operations take:

```python

from imbue.imbue_common.logging import log_span


def save_todo_to_repository(
        todo_repository: TodoRepositoryInterface,
        todo_item: TodoItem,
) -> None:
    with log_span("Saving todo to repository"):
        todo_repository.save_todo(todo_item)
```

`log_span` accepts format args and keyword context args (passed to `logger.contextualize`):

```python
with log_span("Creating agent work directory from source {}", source_path, host=host_name):
    work_dir = host.create_work_dir(source_path)
```

**Do not log at function entry points.** Since logs are placed at the call site (before calling a function), the function itself should not log its own entry. The caller's log already describes what's about to happen:

**Be sparing but comprehensive.** Include enough log statements to understand program flow, but avoid excessive logging. Each log should add meaningful information:

**Do not log in tight loops or frequently-called functions.** Functions called very frequently (e.g., in inner loops) should not log, even at TRACE level, as this creates excessive noise:

**No logs while idle.** Do not emit logs when nothing is happening. Logs should only appear when the program is actively doing something.

Reserve `logger.info` for CLI/user-facing code where messages will be shown to users by default. Library and API code should use `logger.debug` for normal operations:

```python
from loguru import logger


# In CLI code - info is appropriate
def cli_create_todo(title: str) -> None:
    todo = create_todo(title)
    logger.info("Created todo (ID={})", todo.todo_id)


# In library/API code - use log_span for actions
def create_todo(title: str) -> TodoItem:
    with log_span("Creating todo item"):
        todo = TodoItem(title=title)
    return todo
```

## Exception logging

Use `logger.exception` only for unexpected exceptions.

```python
from loguru import logger


class TodoStorageError(TodoAppError):
    """Raised when todo storage operations fail."""

    ...


class TodoNotificationService(MutableModel):
    """Service for sending todo notifications."""

    def send_reminder(self, reminder: TodoReminder) -> None:
        try:
            self._send_notification(reminder)
        except ConnectionError as e:
            logger.exception(e, "Failed to send notification")
            raise
```

## Logging configuration

Use the `setup_logging` helper from `imbue_common` for configuration

```python
from imbue.imbue_common.logging import setup_logging


def main() -> None:
    setup_logging(level="DEBUG")
    ...

```

# Event logging to disk

When persisting structured event data (conversations, agent actions, state transitions, etc.), always use append-only JSONL files following these conventions:

## Standard directory structure

Store event files at `logs/<source>/events.jsonl` where `<source>` is a static, human-readable name describing the category of events:
- Source names should be lowercase, use underscores for multi-word names
- Source names must NOT contain dates, IDs, or dynamically generated values
- Source CAN be nested folders (e.g. `logs/foo/bar/events.jsonl`) with source field `"foo/bar"`, but prefer flat structure when possible

## Standard event envelope

Every JSONL line must include these envelope fields:

```json
{"timestamp": "2026-02-28T12:00:00.123456789Z", "type": "message", "event_id": "evt-1709...", "source": "messages", ...}
```

- `timestamp`: nanosecond-precision UTC ISO 8601 (always include full precision even if the source doesn't provide it)
- `type`: what kind of event this is (e.g. `"conversation_created"`, `"message"`, `"scheduled"`)
- `event_id`: unique identifier for this specific event
- `source`: must match the folder name under `logs/` where this event is stored

## Self-describing events

Include enough context in each line to be self-describing. Every event should have a timestamp, an event type, and enough identifiers (conversation ID, agent name, source, etc.) that you could split the data in different ways later if you change your mind. This is the most important principle: if each line is self-contained, your file organization becomes a performance/convenience choice rather than a correctness one. You should never need to know the name of the file that an event came from.

## Append-only semantics

Event log files are always append-only. Never modify or delete individual lines. If an event needs to be "corrected", append a new event that supersedes it (e.g. a `model_changed` event rather than editing a `conversation_created` event).

## Rotation

Event files can be rotated (by date, by size) if they get too large. Rotation should preserve the file naming convention (`events.jsonl`) and archive old files with a date suffix (e.g. `events.2026-02-28.jsonl.gz`). Not all sources should (or even can) be rotated.

# Configuration

Always use .toml files for configuration

NEVER use .yaml files.

Avoid .json files for configuration--prefer .toml instead

Always place .toml config files in `~/.app_name/config.toml`

Always parse configuration into a structured, fully typed, frozen object

```python
import tomllib

from enum import auto

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.primitives import PositiveInt


class TodoAppConfigurationError(TodoAppError):
    """Raised when configuration cannot be loaded."""

    ...


class LogLevel(UpperCaseStrEnum):
    """Valid logging levels for the application."""

    TRACE = auto()
    DEBUG = auto()
    INFO = auto()
    WARNING = auto()
    ERROR = auto()


class StorageSettings(FrozenModel):
    """Settings for todo storage."""

    storage_directory: Path = Field(description="Where todos are stored")


class NotificationSettings(FrozenModel):
    """Settings for notifications."""

    is_enabled: bool = Field(description="Whether notifications are enabled")


class TodoAppConfiguration(FrozenModel):
    """Configuration settings for the todo application."""

    storage_settings: StorageSettings = Field(description="Storage configuration")
    notification_settings: NotificationSettings = Field(description="Notification configuration")
    max_todos_per_list: PositiveInt = Field(description="Maximum todos per list")
    is_sync_enabled: bool = Field(description="Whether to sync remotely")
    log_level: LogLevel = Field(description="Logging level")


def load_todo_app_configuration() -> TodoAppConfiguration:
    """Raises TodoAppConfigurationError if config is missing or invalid."""
    config_path = Path.home() / ".todo_app" / "config.toml"
    if not config_path.exists():
        raise TodoAppConfigurationError(f"Config not found: {config_path}")
    with config_path.open("rb") as f:
        raw_config = tomllib.load(f)
    return TodoAppConfiguration.model_validate(raw_config)
```

# Command line interfaces

Always use `click` to create commandline interfaces

Always parse the command line arguments into a structured, fully typed, frozen object

Always provide help strings for all arguments

```python
from pathlib import Path

import click

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel


class TodoCliArguments(FrozenModel):
    """Parsed command line arguments for the todo CLI."""

    list_name: TodoListName = Field(description="Name of the todo list to operate on")
    storage_path: Path = Field(description="Path to the todo storage file")
    is_verbose: bool = Field(description="Whether to show detailed output")
    max_display_count: int = Field(description="Maximum todos to display")


@click.command()
@click.option(
    "--list",
    "list_name",
    required=True,
    help="Name of the todo list to display",
)
@click.option(
    "--storage",
    required=True,
    type=click.Path(),
    help="Path to the todo storage file",
)
@click.option(
    "--verbose/--quiet",
    default=False,
    help="Show detailed todo information",
)
@click.option(
    "--max-count",
    default=50,
    help="Maximum number of todos to display",
)
def list_todos(
    list_name: str,
    storage: str,
    verbose: bool,
    max_count: int,
) -> None:
    arguments = TodoCliArguments(
        list_name=TodoListName(list_name),
        storage_path=Path(storage),
        is_verbose=verbose,
        max_display_count=max_count,
    )
    run_list_todos(arguments)
```

# Testing

Always use `pytest` for testing

## High quality tests

Always write tests carefully to avoid race conditions and flaky tests. This means:

- NEVER use time.sleep() (either in tests, or in the actual code). Instead, use polling with timeouts to wait for the required condition to be met
- ALWAYS use `uuid4().hex` to generate unique IDs for any test data that needs an ID or name
- Make every constant globally unique (ex: if running "sleep N" on the command line, use `sleep 36284` instead of something like `sleep 30` to reduce the chances of collisions between test that, for example, check whether this process is still running)

### Testing without mocks

Never use `unittest.mock` (`Mock`, `MagicMock`, `patch`, `create_autospec`, etc.) in tests. These constructs make tests brittle and disconnected from real behavior. They test implementation details rather than actual behavior, and silently pass when the real interface changes.

Never use `monkeypatch.setattr` to replace attributes or functions at runtime. This has the same problems as `unittest.mock` -- it fakes out real objects and breaks the connection between tests and actual behavior.

**Always prefer using real classes and implementations.** Whenever possible, try to break code apart to be functional and testable without needing to mock anything. As a last resport when a real implementation is not feasible in a test (e.g., it requires network access or expensive infrastructure), create a concrete mock implementation of the interface instead.

#### Creating mock implementations

Create concrete mock implementations of interfaces in `mock_*_test.py` files in the same directory as the interface definition.

Mock implementations should:
- Inherit from the interface class (not from `Mock` or `MagicMock`)
- Provide configurable behavior through pydantic `Field` attributes
- Be shared across all test files that need to test against that interface
- Be overridden by specific test files if needed

#### What IS allowed in tests

- `monkeypatch.setenv` / `monkeypatch.delenv` / `monkeypatch.chdir` -- setting environment variables and changing directories is fine since these modify the test environment, not object behavior
- Occasional sparing use of `types.SimpleNamespace` to create a lightweight attribute holder when a full mock implementation would be overkill (e.g., simulating a single boolean property). This should be rare -- prefer real mock implementations
- Using real classes and real implementations whenever possible. Most tests should exercise real code paths

#### What is NOT allowed in tests

- `from unittest.mock import Mock, MagicMock, patch, create_autospec` or any other import from `unittest.mock`
- `monkeypatch.setattr` to replace attributes, methods, or functions on real objects
- `@patch` decorators
- `patch.object()` context managers

### Snapshot testing

Use "snapshot testing" to verify complex outputs and make it easier to verify correctness by looking at the test file.

Prefer to use `inline-snapshot` whenever possible (e.g. whenever the output is small enough to readably fit in the test file)

```python
from inline_snapshot import snapshot


def test_format_todo_for_display_shows_checkbox_and_title() -> None:
    todo = TodoItem(
        todo_id=TodoId.generate(),
        title=TodoTitle("Buy groceries"),
        is_completed=False,
    )

    formatted_output = format_todo_for_display(todo, is_verbose=False)

    assert formatted_output == snapshot("[ ] Buy groceries")


def test_format_todo_for_display_shows_completed_marker_when_done() -> None:
    todo = TodoItem(
        todo_id=TodoId.generate(),
        title=TodoTitle("Send email"),
        is_completed=True,
    )

    formatted_output = format_todo_for_display(todo, is_verbose=False)

    assert formatted_output == snapshot("[x] Send email")
```

If the snapshot data is too large to put in the test file itself, simply save the content to a file and compare the hash of the contents instead:

```python
import hashlib

from pathlib import Path

from inline_snapshot import snapshot


def test_export_large_todo_dataset_to_json_produces_expected_output() -> None:
    """Test that exporting a large dataset produces the expected content."""
    # Create a large dataset with many todos
    large_todo_list = TodoList(
        list_id=TodoListId.generate(),
        name=TodoListName("Large Project"),
        todos=tuple(
            TodoItem(
                todo_id=TodoId.generate(),
                title=TodoTitle(f"Task {i}"),
                description=f"Detailed description for task number {i}",
                status=TodoStatus.PENDING,
                priority=TodoPriority.MEDIUM,
                is_completed=False,
            )
            for i in range(1000)
        ),
    )

    # Export to JSON
    exported_json = export_todo_list_to_json(large_todo_list)

    # Save to a snapshot file in a predictable location for manual review
    snapshot_dir = Path(__file__).parent / "snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot_file = snapshot_dir / "large_todo_export.json"
    snapshot_file.write_text(exported_json)

    # Compare hash instead of the full content
    content_hash = hashlib.sha256(exported_json.encode()).hexdigest()
    assert content_hash == snapshot(
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
```

### Test isolation

Tests should be careful to fully isolate themselves. They should be able to run concurrently, even from within the same pytest process.

This means that base level test fixtures should do things like override the HOME directory to a temp directory. All tests should use unique identifiers for any resources they create and avoid any shared state between tests.

## Test organization

NEVER make classes just to contain the test functions. Instead, always create test functions that begin with `def test_` and then have a nice, long, unique, descriptive name for what is being tested

```python
def test_add_todo_to_list_appends_todo_to_end_of_list() -> None:
    todo_list = TodoList(list_id=TodoListId.generate(), todos=())
    new_todo = create_test_todo(title="New task")

    updated_list = todo_list.with_added_todo(new_todo)

    assert len(updated_list.todos) == 1
    assert updated_list.todos[0] == new_todo
```

Be sure to put tests in the right place (see below for the different types of tests and where they each go)

## Types of tests

There are 4 types of tests: unit tests, integration tests, acceptance tests, and release tests. Each type of test has its own purpose and its own location within the project structure:

1. unit tests: put them in `(src)/**/*_test.py`. They test small, isolated pieces of functionality (ex: a single function or method). They answer the question: "is this code mostly working?" Run locally and are super fast.
2. integration tests: put them in `(src)/**/test_*.py`. They answer the question: "does our program behave in the way that we want?" by testing "end to end" functionality. Run locally with no network access, don't take too long, and are used for calculating coverage.
3. acceptance tests: put them in `(src)/**/test_*.py` and mark with `@pytest.mark.acceptance`. They answer the question: "does the application work under realistic conditions?" by testing with real dependencies (network access, credentials, etc). Run on all branches in CI.
4. release tests: put them in `(src)/**/test_*.py` and mark with `@pytest.mark.release`. They answer the question: "is the application ready for release?" These are more comprehensive acceptance-style tests that only run when pushing to release. The idea is to have them fixed up overnight/before release rather than as a precondition for merging PRs.

### Unit Tests

Unit tests should be fast enough to run frequently, e.g., before committing or while iterating on some new code

Always create unit tests in the same folder as the code under test by creating a file that has the same name, but ends with '_test.py'

```bash
> ls -1
app.py
app_test.py
common.py
common_test.py
```

NEVER make live network requests from unit tests!

NEVER allow flaky or non-deterministic unit tests! If a test fails intermittently, fix it until it is reliable.

Use `pytest-xdist` to run all unit tests in parallel

Always make sure that the entire suite of unit tests runs quickly (< 20 seconds)

Always make sure each individual unit test runs quickly (< 5 seconds)

### Integration Tests

Integration tests are intended to test that a whole piece of program functionality actually works

Always create integration tests the source package folder that contains the main entrypoint being called by the tests, and make sure that the file name *start* with "test_":

```bash
> ls -1
test_tax_calculations.py
test_account_display.py
accounts.py
taxes.py
```

Separate integration tests into different files for logical grouping.

NEVER allow flaky or non-deterministic integration tests! If a test fails intermittently, fix it until it is reliable.

Never make live network requests from integration tests *unless* you are updating snapshots. When updating snapshots, save the resulting data so that it can be used during normal integration test runs:

```python
import json

import httpx
import pytest

from pathlib import Path

from inline_snapshot import snapshot

from imbue.imbue_common.pytest_utils import inline_snapshot_is_updating


def test_sync_todo_list_to_remote_server_handles_response_correctly(
    request: pytest.FixtureRequest,
) -> None:
    """Test that syncing todos processes the server response correctly."""
    # Check if we should make live requests or use cached responses
    is_updating = inline_snapshot_is_updating(request.config)

    # Use a repo-root-relative path for the response file
    response_file_path = snapshot(
        Path("tests/http_responses/sync_todo_list_response.json")
    )

    if is_updating:
        # Make a live HTTP request when updating snapshots
        response = httpx.post(
            "https://api.example.com/v1/sync",
            headers={"Authorization": "Bearer test_api_key"},
            json={"todos": [{"id": "1", "title": "Test todo"}]},
            timeout=30.0,
        )
        response.raise_for_status()
        response_data = response.json()

        # Save the response for future test runs
        response_file_path.parent.mkdir(parents=True, exist_ok=True)
        response_file_path.write_text(json.dumps(response_data, indent=2))
    else:
        # Load the cached response from the saved file
        response_data = json.loads(response_file_path.read_text())

    # Test the actual business logic using the response data
    sync_response = SyncResponse.model_validate(response_data)
    assert sync_response.is_success is True
    assert sync_response.synced_count == snapshot(1)
```

Always make sure each integration test is not too slow (< 60 seconds)

### Acceptance Tests

Acceptance tests verify that the application works under realistic conditions with real dependencies. They can make live web requests, use test credentials, and do basically anything necessary to confirm that the application works as expected.

Create acceptance tests in the source package folder, using files that start with "test_" (same location as integration tests).

Always mark acceptance tests with `@pytest.mark.acceptance`

```python
import pytest


@pytest.mark.acceptance
def test_sync_todos_to_remote_server_succeeds_with_valid_credentials() -> None:
    """Test that we can sync todos to a real remote server."""
    # This test makes real network requests
    ...
```

Acceptance tests run on all branches in CI. They must pass before a PR can be merged.

Acceptance tests can sometimes be flaky. This is ok. Make it possible to easily retry and re-run them if they fail.

### Release Tests

Release tests are comprehensive tests that only run when pushing to the special "release" branch. They verify the application is ready for release and may include slower, more thorough tests that would be too time-consuming to run on every PR.

Create release tests in the source package folder, using files that start with "test_" (same location as integration tests).

Always mark release tests with `@pytest.mark.release`

```python
import pytest


@pytest.mark.release
def test_full_end_to_end_workflow_with_all_providers() -> None:
    """Comprehensive test that exercises all providers."""
    # This test may take longer but ensures full functionality
    ...
```

The full release testing suite is a superset of acceptance tests. When pushing to main, both acceptance and release tests are run. The idea is to have any failures fixed up overnight or before release, rather than blocking every PR merge.

Release tests can sometimes be flaky. This is ok. Make it possible to easily retry and re-run them if they fail.

# Web requests

Always use `httpx` for making web requests

Always use synchronous python code for making requests

```python
import httpx


class TodoSyncError(TodoAppError):
    """Raised when sync operations fail."""

    ...


class SyncResponse(FrozenModel):
    """Response from the sync server."""

    is_success: bool = Field(description="Whether sync succeeded")
    synced_count: int = Field(description="Number of todos synced")


def sync_todo_list_to_remote_server(
    server_url: str,
    todo_list: TodoList,
    api_key: str,
) -> SyncResponse:
    """Raises TodoSyncError if the sync request fails."""
    url = f"{server_url}/api/v1/sync"
    headers = {"Authorization": f"Bearer {api_key}"}
    payload = todo_list.model_dump()
    response = httpx.post(url, json=payload, headers=headers, timeout=30.0)
    response.raise_for_status()
    return SyncResponse.model_validate(response.json())
```

# Marking future work

Planned features should raise `NotImplementedError`, and docs referring to them should mark them with [future].

Cleanup tasks are marked with TODO or FIXME.

# Misc

Never use `async` or `asyncio`

Never use `pandas`. Prefer `polars` instead

Never use `eval` or `exec` unless explicitly instructed to do so

Never use dataclasses or named tuples

# Compiling and verifying this style guide

This style guide contains Python code examples that can be compiled into a single file to verify they are syntactically valid and work together correctly.

To compile the style guide examples:

```bash
uv run python scripts/compile_style_guide.py
```

This will extract all Python code blocks from this markdown file and combine them into `scripts/style_guide.py`. The compilation process:

1. Extracts all Python code blocks from the markdown
2. Collects all imports and places them at the top of the file
3. Removes any imports from `todo_app.*` (since objects should be defined within the examples)
4. Removes any pytest skip decorators
5. Adds `from __future__ import annotations` to handle forward references
6. Concatenates all code blocks in order

To verify the compiled file is syntactically valid:

```bash
uv run python scripts/style_guide.py
```

If this command runs without errors, all examples in the style guide are syntactically correct and compatible with each other.
