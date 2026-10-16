import re


class DomainError(Exception):
    """Base for errors core services raise. The API maps subclasses to status codes.

    `details` carry what a caller can act on, e.g. NotFound("unknown_workspace", available=[...]). The MCP server
    returns them to the model as `{"error": code, **details}`.
    """

    def __init__(self, message: str = "", **details):
        super().__init__(message)
        self.details = details


class NotFound(DomainError):
    pass


class InvalidInput(DomainError):
    pass


class Conflict(DomainError):
    """The request is valid but the resource isn't in a state that allows it (e.g. paper_not_ready)."""


CODE = re.compile(r"[a-z]+(?:_[a-z]+)+")  # a message like unknown_workspace is itself the error code
KINDS = {NotFound: "not_found", InvalidInput: "invalid_input", Conflict: "conflict"}


def error_code(exc: DomainError) -> str:
    """The code a client sees: the message when it is one, else the error's kind."""
    message = str(exc)
    return message if CODE.fullmatch(message) else KINDS.get(type(exc), "invalid_input")
