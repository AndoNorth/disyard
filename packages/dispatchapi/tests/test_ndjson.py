"""The NDJSON body of a batch submit call: every line read whatever its framing and however the
bytes arrive, the limits of a call, and the answer written with chunked transfer encoding."""

import io
import json
import socket
import time

import pytest
from dsy_dispatchapi.limits import Limits
from dsy_dispatchapi.models import BatchEntry, BatchHeader, EntryAnswer, SubmitSummary, SummaryLine
from dsy_dispatchapi.ndjson import Reader, Rejected, Stop, Writer, request_body
from hypothesis import given
from hypothesis import strategies as st

HEADER = {"batch_key": "nightly", "template": {"schema": "urn:dsy:schema:job:1.0"}}
JOB_ID = "6f1c1f2e-8d5b-5c3e-9a1f-2b7d4e6a8c90"
TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
DEFAULTS = Limits()


class Pieces(io.RawIOBase):
    """A stream that hands its bytes over in the given pieces, as a socket does."""

    def __init__(self, pieces: list[bytes]):
        self._pieces = [piece for piece in pieces if piece]

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        if not self._pieces:
            return 0
        piece = self._pieces[0][: len(buffer)]
        self._pieces[0] = self._pieces[0][len(piece) :]
        if not self._pieces[0]:
            self._pieces.pop(0)
        buffer[: len(piece)] = piece
        return len(piece)


def split(data: bytes, cuts: list[int]) -> list[bytes]:
    bounds = sorted({0, len(data), *(cut % (len(data) + 1) for cut in cuts)})
    return [data[start:end] for start, end in zip(bounds, bounds[1:], strict=False)]


def line(data) -> bytes:
    return json.dumps(data).encode() + b"\n"


def body(*lines: bytes) -> bytes:
    return line(HEADER) + b"".join(lines)


def entry(index: int, **fields) -> bytes:
    return line({"index": index, "seed": index, "parameters": {}, **fields})


def chunked(data: bytes, cuts: list[int]) -> bytes:
    return b"".join(b"%x\r\n%b\r\n" % (len(p), p) for p in split(data, cuts)) + b"0\r\n\r\n"


def read(data: bytes, limits: Limits = DEFAULTS) -> tuple[BatchHeader | Stop, list]:
    rfile = io.BufferedReader(Pieces([data]))
    calls = Reader(request_body(rfile, {"Content-Length": str(len(data))}), limits)
    first = calls.header()
    return first, list(calls.entries()) if isinstance(first, BatchHeader) else []


@given(
    seeds=st.lists(st.integers(0, 2**53 - 1), max_size=40),
    labels=st.lists(st.text(max_size=30), max_size=40),
    socket_cuts=st.lists(st.integers(0, 10_000), max_size=30),
    chunk_cuts=st.lists(st.integers(0, 10_000), max_size=30),
    framing=st.sampled_from(["length", "chunked"]),
)
def test_every_line_whatever_the_byte_boundaries(seeds, labels, socket_cuts, chunk_cuts, framing):
    sent = [
        {"index": index, "seed": seed, "parameters": {"label": label}}
        for index, (seed, label) in enumerate(zip(seeds, labels, strict=False))
    ]
    data = line(HEADER) + b"".join(line(e) for e in sent)
    if framing == "chunked":
        wire, headers = chunked(data, chunk_cuts), {"Transfer-Encoding": "chunked"}
    else:
        wire, headers = data, {"Content-Length": str(len(data))}
    rfile = io.BufferedReader(Pieces(split(wire, socket_cuts)), buffer_size=7)

    calls = Reader(request_body(rfile, headers), Limits())

    assert calls.header() == BatchHeader.model_validate(HEADER)
    assert list(calls.entries()) == [BatchEntry.model_validate(e) for e in sent]


def test_header_too_large():
    fits = len(line(HEADER)) - 1

    assert read(body(entry(0)), Limits(header_line_bytes=fits - 1))[0] == Stop(
        "HEADER_TOO_LARGE", f"the header line is above {fits - 1} bytes"
    )
    first, entries = read(body(entry(0)), Limits(header_line_bytes=fits))
    assert isinstance(first, BatchHeader)
    assert len(entries) == 1


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "the body is empty"),
        (line(HEADER)[:-1], "the header line has no end"),
        (b"[1]\n", "the line is not a JSON object"),
        (b'{"batch_key": "k", "template": {"x": NaN}}\n', "NaN is not JSON"),
        (b"\xff\n", "can't decode"),
        (line({"template": {}}), "batch_key: Field required"),
        (line({**HEADER, "extra": 1}), "extra: Extra inputs are not permitted"),
    ],
)
def test_invalid_header(data, message):
    first, _ = read(data)

    assert isinstance(first, Stop)
    assert first.code == "INVALID_HEADER"
    assert message in first.message


def test_entry_too_large_is_an_entry_result():
    big = entry(1, parameters={"label": "x" * 100})
    limits = Limits(entry_line_bytes=len(big) - 2)

    _, entries = read(body(entry(0), big, entry(2)), limits)

    assert entries == [
        BatchEntry.model_validate(json.loads(entry(0))),
        Rejected("ENTRY_TOO_LARGE", f"the entry line is above {len(big) - 2} bytes"),
        BatchEntry.model_validate(json.loads(entry(2))),
    ]
    assert read(body(big), Limits(entry_line_bytes=len(big) - 1))[1][0].index == 1


def test_truncated_last_line_is_malformed():
    _, entries = read(body(entry(0), entry(1)[:-1]))

    assert entries[0] == BatchEntry.model_validate(json.loads(entry(0)))
    assert entries[1] == Rejected(
        "MALFORMED_LINE", "the last line has no end, so it may be cut short"
    )


def test_a_body_cut_short_ends_where_it_was_cut():
    data = body(entry(0), entry(1))
    rfile = io.BufferedReader(Pieces([data[:-5]]))
    calls = Reader(request_body(rfile, {"Content-Length": str(len(data))}), Limits())
    calls.header()

    entries = list(calls.entries())

    assert [type(e) for e in entries] == [BatchEntry, Rejected]
    assert entries[1].reason == "MALFORMED_LINE"


@pytest.mark.parametrize(
    ("data", "reason", "index"),
    [
        (b"\n", "Expecting value", None),
        (b"[1]\n", "the line is not a JSON object", None),
        (b"not json\n", "Expecting value", None),
        (b'{"index": 3, "seed": Infinity, "parameters": {}}\n', "Infinity is not JSON", None),
        (line({"index": 3, "seed": -1, "parameters": {}}), "seed: Input should be", 3),
        (line({"index": True, "seed": 1, "parameters": {}}), "index: Input should be", None),
        (line({"index": 100_000, "seed": 1, "parameters": {}}), "index: Input should be", None),
        (line({"index": 4, "seed": 1}), "parameters: Field required", 4),
    ],
)
def test_malformed_entry_names_its_index_when_it_can(data, reason, index):
    _, entries = read(body(data))

    assert entries[0].reason == "MALFORMED_LINE"
    assert reason in entries[0].message
    assert entries[0].index == index


def test_too_many_entries_answers_every_earlier_entry():
    _, entries = read(body(*(entry(i) for i in range(5))), Limits(entries=3))

    assert entries == [
        *(BatchEntry.model_validate(json.loads(entry(i))) for i in range(3)),
        Stop("TOO_MANY_ENTRIES", "a call holds at most 3 entries"),
    ]


def test_the_limit_of_entries_is_not_a_stop():
    _, entries = read(body(*(entry(i) for i in range(3))), Limits(entries=3))

    assert len(entries) == 3
    assert not any(isinstance(e, Stop) for e in entries)


def test_an_entry_is_read_before_the_next_arrives():
    server, client = socket.socketpair()
    with server, client:
        client.sendall(body(entry(0)))
        rfile = server.makefile("rb")
        calls = Reader(request_body(rfile, {"Content-Length": "100000"}), Limits(), server)
        calls.header()

        assert next(calls.entries()) == BatchEntry.model_validate(json.loads(entry(0)))


def test_idle_timeout():
    server, client = socket.socketpair()
    with server, client:
        client.sendall(body(entry(0)) + entry(1)[:5])
        rfile = server.makefile("rb")
        limits = Limits(idle_timeout_s=0.2)
        calls = Reader(request_body(rfile, {"Content-Length": "100000"}), limits, server)
        calls.header()

        started = time.monotonic()
        entries = list(calls.entries())

        assert time.monotonic() - started < 5
        assert entries == [
            BatchEntry.model_validate(json.loads(entry(0))),
            Stop("IDLE_TIMEOUT", "no byte arrived in the idle timeout"),
        ]
        assert server.gettimeout() == 0.2


def test_idle_timeout_before_the_header():
    server, client = socket.socketpair()
    with server, client:
        rfile = server.makefile("rb")
        calls = Reader(
            request_body(rfile, {"Content-Length": "10"}), Limits(idle_timeout_s=0.2), server
        )

        assert calls.header() == Stop(
            "IDLE_TIMEOUT", "no byte of the header arrived in the idle timeout"
        )


def test_writer_answers_with_chunked_transfer_encoding():
    answers = [
        EntryAnswer(index=0, outcome="accepted", job_id=JOB_ID, trace_id=TRACE_ID),
        EntryAnswer(outcome="rejected", reason="MALFORMED_LINE"),
    ]
    summary = SummaryLine(
        summary=SubmitSummary(batch_id=JOB_ID, accepted=1, duplicate=0, rejected=1)
    )
    wfile = io.BytesIO()
    writer = Writer(wfile, Limits())

    writer.write(*answers)
    writer.write()
    writer.write(summary)
    writer.end()

    decoded = io.BufferedReader(Pieces([wfile.getvalue()]))
    lines = request_body(decoded, {"Transfer-Encoding": "chunked"}).read().splitlines()
    assert [json.loads(text) for text in lines] == [
        {"index": 0, "outcome": "accepted", "job_id": JOB_ID, "trace_id": TRACE_ID},
        {"outcome": "rejected", "reason": "MALFORMED_LINE"},
        {"summary": {"batch_id": JOB_ID, "accepted": 1, "duplicate": 0, "rejected": 1}},
    ]
    assert wfile.getvalue().count(b"\r\n0\r\n\r\n") == 1
    assert wfile.getvalue().endswith(b"0\r\n\r\n")


def test_writer_times_out_when_the_client_reads_nothing():
    server, client = socket.socketpair()
    with server, client:
        writer = Writer(server.makefile("wb"), Limits(write_timeout_s=0.2), server)
        answer = EntryAnswer(index=0, outcome="accepted", job_id=JOB_ID, trace_id=TRACE_ID)

        with pytest.raises(TimeoutError):
            for _ in range(1_000_000):
                writer.write(*[answer] * 100)


@pytest.mark.parametrize(
    "wire",
    [
        b"zz\r\n" + body(entry(0)),
        b"-5\r\n" + body(entry(0)),
        b"5\r\nabcdeXX",
    ],
)
def test_broken_chunk_framing_ends_the_body(wire):
    rfile = io.BufferedReader(Pieces([wire, b"never read\n"]))

    assert request_body(rfile, {"Transfer-Encoding": "chunked"}).read() in (b"", b"abcde")


def test_chunk_extensions_and_trailers_are_read_past():
    data = body(entry(0))
    wire = b"%x;name=value\r\n%b\r\n0\r\nTrailer: x\r\n\r\nnext request" % (len(data), data)
    rfile = io.BufferedReader(Pieces([wire]))

    assert request_body(rfile, {"Transfer-Encoding": "chunked"}).read() == data
    assert rfile.read() == b"next request"
