from pydantic import BaseModel


def assert_model_is_frozen(model_class: type[BaseModel]) -> None:
    """Assert that a pydantic model is still declared immutable.

    Pydantic merges a subclass's ``model_config`` into its parent's, so an explicit
    ``frozen=False`` is the one thing that un-freezes a ``FrozenModel`` subclass --
    and it would silently stop the type checker reporting mutations of that model
    everywhere else. The other half of the claim, that pydantic enforces the config
    at all, belongs to ``FrozenModel`` rather than to any one model and is exercised
    once in ``frozen_model_test.py``.
    """
    assert model_class.model_config.get("frozen") is True, f"{model_class.__name__} is not declared frozen"
