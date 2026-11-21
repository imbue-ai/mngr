class AppSdkError(Exception):
    """Base of every error the library raises."""


class InvalidRequestBodyError(AppSdkError, ValueError):
    """A request body that is not a JSON object, or not the shape its route's model requires (a 400)."""


class RouteModelsMismatchError(AppSdkError, RuntimeError):
    """A route parsed or answered a model its app's route table does not name for it: the document would lie."""


class RouteTableConflictError(AppSdkError, ValueError):
    """Two route tables merged into one app's table name the same route."""


class InvalidIdentityValueError(AppSdkError, ValueError):
    """A value the identity header carries (a user id, a participant key) does not satisfy its rule."""


class InvalidIdentityHeaderError(InvalidIdentityValueError):
    """A present ``X-Imbue-Identity`` header that does not parse: never a proxy's, so the request is refused rather
    than read as the owner's."""


class ShellUnreachableError(AppSdkError, ConnectionError):
    """The workspace shell did not answer: it is down, restarting, or this app is running outside a workspace."""


class AppNotRegisteredError(AppSdkError, KeyError):
    """The workspace shell lists no app of the given name."""
