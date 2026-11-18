"""The NDJSON body of a batch submit call: the request read as a stream of lines under the limits
of the call, and the answer written line by line with chunked transfer encoding.

The reader holds one line at a time, so a call of any length reads in bounded memory, and it reads
the next line only when asked, so the server answers each chunk of entries before it reads more.
"""

import io
import json
import re
import socket
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import BinaryIO

from pydantic import BaseModel, ValidationError

from wdf_dispatchapi.limits import Limits
from wdf_dispatchapi.models import ENTRIES_MAX, BatchEntry, BatchHeader

# A chunk size line and a trailer line are short, and a longer one is a broken body.
_FRAMING_LINE_MAX = 1_024
_CHUNK_SIZE = re.compile(rb"[0-9A-Fa-f]{1,16}")
_DRAIN = 65_536


@dataclass(frozen=True)
class Stop:
    """The call stops here: what was read before it stands, and nothing after it is read."""

    code: str
    message: str


@dataclass(frozen=True)
class Rejected:
    """An entry line that is answered `rejected` without reaching the submit sequence."""

    reason: str
    message: str
    index: int | None = None


class _Length(io.RawIOBase):
    def __init__(self, rfile: BinaryIO, length: int):
        self._rfile = rfile
        self._left = length

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        if not self._left:
            return 0
        data = self._rfile.read1(min(len(buffer), self._left))
        # A body cut short ends where it was cut.
        self._left = self._left - len(data) if data else 0
        buffer[: len(data)] = data
        return len(data)


class _Chunked(io.RawIOBase):
    def __init__(self, rfile: BinaryIO):
        self._rfile = rfile
        self._left = 0
        self._done = False

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        if not self._left and not self._done:
            self._next_chunk()
        if self._done:
            return 0
        data = self._rfile.read1(min(len(buffer), self._left))
        if not data:
            self._done = True
            return 0
        self._left -= len(data)
        if not self._left and self._rfile.readline(_FRAMING_LINE_MAX).rstrip(b"\r\n"):
            self._done = True
        buffer[: len(data)] = data
        return len(data)

    def _next_chunk(self) -> None:
        # A body cut short or framed wrongly ends where the framing broke.
        size = self._rfile.readline(_FRAMING_LINE_MAX).split(b";", 1)[0].strip()
        if not _CHUNK_SIZE.fullmatch(size):
            self._done = True
        elif not (left := int(size, 16)):
            self._done = True
            while self._rfile.readline(_FRAMING_LINE_MAX).rstrip(b"\r\n"):
                pass
        else:
            self._left = left


def request_body(rfile: BinaryIO, headers: Mapping[str, str]) -> BinaryIO:
    """The request body of an HTTP/1.1 request, framed by `Content-Length` or chunked."""
    if "chunked" in headers.get("Transfer-Encoding", "").lower():
        return io.BufferedReader(_Chunked(rfile))
    try:
        length = max(int(headers.get("Content-Length", "0")), 0)
    except ValueError:
        length = 0
    return io.BufferedReader(_Length(rfile, length))


def _reject_constant(name: str):
    raise ValueError(f"{name} is not JSON")


def _object(line: bytes) -> dict:
    data = json.loads(line.decode(), parse_constant=_reject_constant)
    if not isinstance(data, dict):
        raise ValueError("the line is not a JSON object")
    return data


def _issue(error: ValidationError) -> str:
    first = error.errors()[0]
    where = "/".join(str(part) for part in first["loc"])
    return f"{where}: {first['msg']}" if where else first["msg"]


class Reader:
    """The header and then the entries of a batch submit body.

    With the connection's socket, each read waits at most the idle timeout of the limits.
    """

    def __init__(self, body: BinaryIO, limits: Limits, connection: socket.socket | None = None):
        self._body = body
        self._limits = limits
        self._connection = connection

    def _line(self, limit: int) -> tuple[bytes, bool]:
        """The next line with its newline when it has one, and whether it fits the limit. A line
        over the limit is read to its end and dropped."""
        if self._connection is not None:
            self._connection.settimeout(self._limits.idle_timeout_s)
        line = self._body.readline(limit + 1)
        if len(line) <= limit or line.endswith(b"\n"):
            return line, True
        while (rest := self._body.readline(_DRAIN)) and not rest.endswith(b"\n"):
            pass
        return line, False

    def header(self) -> BatchHeader | Stop:
        try:
            line, fits = self._line(self._limits.header_line_bytes)
        except TimeoutError:
            return Stop("IDLE_TIMEOUT", "no byte of the header arrived in the idle timeout")
        if not fits:
            limit = self._limits.header_line_bytes
            return Stop("HEADER_TOO_LARGE", f"the header line is above {limit} bytes")
        if not line:
            return Stop("INVALID_HEADER", "the body is empty")
        if not line.endswith(b"\n"):
            return Stop("INVALID_HEADER", "the header line has no end")
        try:
            return BatchHeader.model_validate(_object(line))
        except ValidationError as error:
            return Stop("INVALID_HEADER", _issue(error))
        except ValueError as error:
            return Stop("INVALID_HEADER", str(error))

    def entries(self) -> Iterator[BatchEntry | Rejected | Stop]:
        """Each entry line in order, parsed or rejected, then a Stop when the call ends early."""
        count = 0
        while True:
            try:
                line, fits = self._line(self._limits.entry_line_bytes)
            except TimeoutError:
                yield Stop("IDLE_TIMEOUT", "no byte arrived in the idle timeout")
                return
            if not line:
                return
            if count == self._limits.entries:
                yield Stop("TOO_MANY_ENTRIES", f"a call holds at most {count} entries")
                return
            count += 1
            yield self._entry(line, fits)

    def _entry(self, line: bytes, fits: bool) -> BatchEntry | Rejected:
        if not fits:
            limit = self._limits.entry_line_bytes
            return Rejected("ENTRY_TOO_LARGE", f"the entry line is above {limit} bytes")
        if not line.endswith(b"\n"):
            return Rejected("MALFORMED_LINE", "the last line has no end, so it may be cut short")
        try:
            data = _object(line)
        except ValueError as error:
            return Rejected("MALFORMED_LINE", str(error))
        try:
            return BatchEntry.model_validate(data)
        except ValidationError as error:
            index = data.get("index")
            known = type(index) is int and 0 <= index < ENTRIES_MAX
            return Rejected("MALFORMED_LINE", _issue(error), index if known else None)


class Writer:
    """An NDJSON answer body with chunked transfer encoding, after the caller sent the headers.

    With the connection's socket, each write waits at most the write timeout of the limits, and
    a write past it raises TimeoutError.
    """

    def __init__(self, wfile: BinaryIO, limits: Limits, connection: socket.socket | None = None):
        self._wfile = wfile
        self._limits = limits
        self._connection = connection

    def _send(self, data: bytes) -> None:
        if self._connection is not None:
            self._connection.settimeout(self._limits.write_timeout_s)
        self._wfile.write(data)
        self._wfile.flush()

    def write(self, *lines: BaseModel) -> None:
        """Writes the lines as one chunk, each with the fields that were set."""
        data = b"".join(line.model_dump_json(exclude_unset=True).encode() + b"\n" for line in lines)
        if data:
            self._send(b"%x\r\n%b\r\n" % (len(data), data))

    def end(self) -> None:
        self._send(b"0\r\n\r\n")
