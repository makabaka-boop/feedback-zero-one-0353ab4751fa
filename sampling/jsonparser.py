"""Minimal strict JSON parser implemented from scratch.

Why not use the standard library's ``json`` module?  The scheduler must

* report zero-based *byte* offsets for syntax errors,
* reject duplicate object members while naming the offending member path,
* keep booleans strictly distinct from integers (``True``/``False`` must
  never be accepted as window bounds or counts).

A hand-written recursive descent parser gives all three directly.  The
grammar follows RFC 8259.
"""

from __future__ import annotations

from typing import Any, List

from .errors import JsonSyntaxError

# Structural characters that may legally appear after a value.
_DELIMITERS = b" \t\r\n,}]:"


class _Parser:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.n = len(data)
        self.i = 0
        # Path stack of member names / array indices, used to annotate errors.
        self.path: List[str] = ["$"]

    # -- low level helpers -------------------------------------------------

    def error(self, message: str, at: int | None = None) -> "JsonSyntaxError":
        return JsonSyntaxError(
            self.n if at is None else at, message, "".join(self.path)
        )

    def skip_ws(self) -> None:
        data = self.data
        while self.i < self.n and data[self.i] in b" \t\r\n":
            self.i += 1

    def peek(self) -> int:
        return self.data[self.i] if self.i < self.n else -1

    # -- entry point --------------------------------------------------------

    def parse(self) -> Any:
        self.skip_ws()
        if self.i >= self.n:
            raise self.error("empty document", 0)
        value = self.parse_value()
        self.skip_ws()
        if self.i != self.n:
            raise self.error("trailing data after JSON value", self.i)
        return value

    # -- values -------------------------------------------------------------

    def parse_value(self) -> Any:
        c = self.peek()
        if c == ord("{"):
            return self.parse_object()
        if c == ord("["):
            return self.parse_array()
        if c == ord('"'):
            return self.parse_string()
        if c == ord("t"):
            return self.parse_literal(b"true", True)
        if c == ord("f"):
            return self.parse_literal(b"false", False)
        if c == ord("n"):
            return self.parse_literal(b"null", None)
        if c == ord("-") or 48 <= c <= 57:
            return self.parse_number()
        raise self.error("expected JSON value", self.i)

    def parse_literal(self, literal: bytes, value: Any) -> Any:
        start = self.i
        if not self.data.startswith(literal, start):
            raise self.error(f"invalid literal, expected {literal.decode()}", start)
        self.i += len(literal)
        return value

    def parse_number(self) -> int | float:
        start = self.i
        data = self.data
        is_float = False
        if self.peek() == ord("-"):
            self.i += 1
            if self.i >= self.n:
                raise self.error("unexpected end of number", start)
        # integer part: 0 | digit1-9 *digit
        if self.peek() == ord("0"):
            self.i += 1
        elif 49 <= self.peek() <= 57:
            while 48 <= self.peek() <= 57:
                self.i += 1
        else:
            raise self.error("invalid number", self.i)
        # fraction
        if self.peek() == ord("."):
            is_float = True
            self.i += 1
            if not (48 <= self.peek() <= 57):
                raise self.error("expected fractional digit", self.i)
            while 48 <= self.peek() <= 57:
                self.i += 1
        # exponent
        if self.peek() in (ord("e"), ord("E")):
            is_float = True
            self.i += 1
            if self.peek() in (ord("+"), ord("-")):
                self.i += 1
            if not (48 <= self.peek() <= 57):
                raise self.error("expected exponent digit", self.i)
            while 48 <= self.peek() <= 57:
                self.i += 1
        text = data[start:self.i].decode("ascii")
        return float(text) if is_float else int(text)

    def parse_string(self) -> str:
        # Open quote.
        start = self.i
        self.i += 1
        out: List[str] = []
        data = self.data
        while True:
            if self.i >= self.n:
                raise self.error("unterminated string", start)
            c = data[self.i]
            if c == ord('"'):
                self.i += 1
                return "".join(out)
            if c < 0x20:
                raise self.error("unescaped control character in string", self.i)
            if c == ord("\\"):
                self.i += 1
                if self.i >= self.n:
                    raise self.error("unterminated escape", self.i)
                e = data[self.i]
                simple = {
                    ord('"'): '"',
                    ord("\\"): "\\",
                    ord("/"): "/",
                    ord("b"): "\b",
                    ord("f"): "\f",
                    ord("n"): "\n",
                    ord("r"): "\r",
                    ord("t"): "\t",
                }
                if e in simple:
                    out.append(simple[e])
                    self.i += 1
                elif e == ord("u"):
                    cp = self.parse_hex4()
                    if 0xD800 <= cp <= 0xDBFF:
                        # High surrogate: must be followed by a low surrogate.
                        if data[self.i:self.i + 2] != b"\\u":
                            raise self.error(
                                "high surrogate not followed by low surrogate",
                                self.i,
                            )
                        self.i += 2
                        low = self.parse_hex4()
                        if not (0xDC00 <= low <= 0xDFFF):
                            raise self.error("invalid low surrogate", self.i - 4)
                        cp = 0x10000 + ((cp - 0xD800) << 10) + (low - 0xDC00)
                    elif 0xDC00 <= cp <= 0xDFFF:
                        raise self.error("unexpected low surrogate", self.i - 4)
                    out.append(chr(cp))
                else:
                    raise self.error("invalid escape sequence", self.i)
            else:
                # Collect a run of raw UTF-8 bytes and decode together.
                run_start = self.i
                while (
                    self.i < self.n
                    and data[self.i] != ord('"')
                    and data[self.i] != ord("\\")
                    and data[self.i] >= 0x20
                ):
                    self.i += 1
                try:
                    out.append(data[run_start:self.i].decode("utf-8"))
                except UnicodeDecodeError as exc:
                    raise self.error("invalid UTF-8 in string", run_start + exc.start)

    def parse_hex4(self) -> int:
        # Caller is positioned on the 'u' of \uXXXX.
        self.i += 1
        if self.n - self.i < 4:
            raise self.error("incomplete unicode escape", self.i)
        chunk = self.data[self.i:self.i + 4]
        try:
            value = int(chunk, 16)
        except ValueError:
            raise self.error("invalid unicode escape", self.i)
        self.i += 4
        return value

    def parse_array(self) -> List[Any]:
        open_at = self.i
        self.i += 1  # consume [
        result: List[Any] = []
        self.skip_ws()
        if self.peek() == ord("]"):
            self.i += 1
            return result
        while True:
            self.skip_ws()
            self.path.append(f"[{len(result)}]")
            result.append(self.parse_value())
            self.path.pop()
            self.skip_ws()
            c = self.peek()
            if c == ord(","):
                self.i += 1
                self.skip_ws()
                # A comma must be followed by another element.
                if self.peek() in (ord(","), ord("]")):
                    raise self.error("expected array element", self.i)
                continue
            if c == ord("]"):
                self.i += 1
                return result
            raise self.error("expected ',' or ']' in array", self.i if self.i < self.n else open_at)

    def parse_object(self) -> dict:
        open_at = self.i
        self.i += 1  # consume {
        result: dict = {}
        self.skip_ws()
        if self.peek() == ord("}"):
            self.i += 1
            return result
        while True:
            self.skip_ws()
            if self.peek() != ord('"'):
                raise self.error("expected string member name", self.i)
            key = self.parse_string()
            self.skip_ws()
            if self.peek() != ord(":"):
                raise self.error("expected ':' after member name", self.i)
            self.i += 1
            self.skip_ws()
            if key in result:
                raise self.error(
                    f"duplicate member {key!r}", self.i
                )
            self.path.append(f"[{_format_key(key)}]")
            result[key] = self.parse_value()
            self.path.pop()
            self.skip_ws()
            c = self.peek()
            if c == ord(","):
                self.i += 1
                self.skip_ws()
                if self.peek() in (ord(","), ord("}")):
                    raise self.error("expected object member", self.i)
                continue
            if c == ord("}"):
                self.i += 1
                return result
            raise self.error("expected ',' or '}' in object", self.i if self.i < self.n else open_at)


def _format_key(key: str) -> str:
    """Render an object key the way it appears inside a JSONPath segment."""
    import json as _json

    return _json.dumps(key, ensure_ascii=False)


def parse_json(data: bytes) -> Any:
    """Parse a JSON document, raising :class:`JsonSyntaxError` on failure."""
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("parse_json expects bytes")
    return _Parser(bytes(data)).parse()
