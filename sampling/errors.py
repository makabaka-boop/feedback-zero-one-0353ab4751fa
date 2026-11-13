"""Error hierarchy for the sampling scheduler.

All failures raised by the package derive from :class:`SamplingError` so the
CLI can distinguish expected validation failures from unexpected bugs.
"""


class SamplingError(Exception):
    """Base class for every error reported to the caller."""


class JsonSyntaxError(SamplingError):
    """The input document is not syntactically valid JSON.

    ``byte`` is a zero-based byte offset into the raw input document.
    """

    def __init__(self, byte: int, message: str, path: str = "$"):
        self.byte = byte
        self.message = message
        self.path = path
        super().__init__(f"syntax error at byte {byte} ({path}): {message}")


class ValidationError(SamplingError):
    """The input is well-formed JSON but violates the documented schema.

    ``path`` uses JSONPath-like notation with zero-based array indices,
    e.g. ``$.overrides["2024-01-01T00:00:00"]`` or ``$.window[0]``.
    """

    def __init__(self, path: str, message: str):
        self.path = path
        self.message = message
        super().__init__(f"{path}: {message}")


class RRuleError(SamplingError):
    """The RRULE string cannot be parsed or violates the supported subset.

    ``byte`` is the zero-based byte offset inside the RRULE value (the rule
    only ever contains ASCII, so character and byte offsets coincide).
    """

    def __init__(self, byte: int, message: str):
        self.byte = byte
        self.message = message
        super().__init__(f"$.rule (byte {byte}): {message}")
