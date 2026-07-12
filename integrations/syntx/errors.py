"""Shared SyntX integration exceptions."""


class SyntXBridgeError(RuntimeError):
    """Raised when the SyntX bridge returns an error or is unreachable."""


class ModelMismatchError(SyntXBridgeError):
    """Raised when strict model enforcement fails."""
