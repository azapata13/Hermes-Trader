"""Schema-versioned msgpack codec for Hermès recordings (``.hrec``).

File layout
-----------
    MAGIC (8 bytes: b"HERMREC\\x01")
    record*    where record = uint32 big-endian length + msgpack payload

Record payloads (msgpack arrays; first element = record kind):
    [KIND_HEADER, {...}]                                  first record of every part file
    [KIND_RAW, type_code, v1, v2, ...]                    one RawEvent; values in declared field order
    [KIND_GAP, first_seq, last_seq, count, reason, mono_ns, wall_ns]   RecordingGap (not a raw event)
    [KIND_FOOTER, {...}]                                  written on clean close of a part

The header embeds the full type table ``{type_code: [class_name, [field, ...]]}`` so a
recording is self-describing; the reader decodes by field NAME and refuses unknown schema
versions. Any change to raw event fields MUST bump ``SCHEMA_VERSION``.

Value encoding: ``Decimal`` -> msgpack ExtType(1, str) (exact); tuples -> arrays (restored as
tuples on decode). No pickle, no code execution on read.
"""

from __future__ import annotations

import dataclasses
import struct
from decimal import Decimal
from typing import Any, Iterator

import msgpack

from hermes.ibkr import raw_events as R

MAGIC = b"HERMREC\x01"
SCHEMA_VERSION = 1
FORMAT_NAME = "hermes-raw"

KIND_HEADER = 0
KIND_RAW = 1
KIND_GAP = 2
KIND_FOOTER = 3

_EXT_DECIMAL = 1
_LEN = struct.Struct(">I")
MAX_RECORD_BYTES = 16 * 1024 * 1024

# Stable type codes. NEVER renumber; append only (and bump SCHEMA_VERSION on field changes).
RAW_TYPE_CODES: dict[type, int] = {
    R.RawMarketDepth: 1,
    R.RawTickByTickAllLast: 2,
    R.RawTickByTickBidAsk: 3,
    R.RawTickPrice: 4,
    R.RawTickSize: 5,
    R.RawContractDetails: 6,
    R.RawContractDetailsEnd: 7,
    R.RawMarketRule: 8,
    R.RawError: 9,
    R.RawMarketDataType: 10,
    R.RawCurrentTime: 11,
    R.RawNextValidId: 12,
    R.RawConnectAck: 13,
    R.RawConnectionClosed: 14,
    R.RawTimerTick: 15,
    R.RawRequestIssued: 16,
    R.RawRequestFailed: 17,
    R.RawControl: 18,
    R.RawSessionMarker: 19,
}
_FIELDS: dict[type, tuple[str, ...]] = {cls: tuple(f.name for f in dataclasses.fields(cls)) for cls in RAW_TYPE_CODES}


class CodecError(ValueError):
    pass


def type_table() -> dict[int, list]:
    return {code: [cls.__name__, list(_FIELDS[cls])] for cls, code in RAW_TYPE_CODES.items()}


def _default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return msgpack.ExtType(_EXT_DECIMAL, str(obj).encode("ascii"))
    raise TypeError(f"cannot encode {type(obj).__name__}")


def _ext_hook(code: int, data: bytes) -> Any:
    if code == _EXT_DECIMAL:
        return Decimal(data.decode("ascii"))
    return msgpack.ExtType(code, data)


def _tuplify(v: Any) -> Any:
    if isinstance(v, list):
        return tuple(_tuplify(x) for x in v)
    return v


class Encoder:
    """Not thread-safe; owned by the recorder writer thread."""

    def __init__(self) -> None:
        self._packer = msgpack.Packer(default=_default, use_bin_type=True)

    def _frame(self, payload: bytes) -> bytes:
        return _LEN.pack(len(payload)) + payload

    def header(self, meta: dict) -> bytes:
        body = {"format": FORMAT_NAME, "schema_version": SCHEMA_VERSION, "types": type_table(), **meta}
        return self._frame(self._packer.pack([KIND_HEADER, body]))

    def raw(self, ev: R.RawEvent) -> bytes:
        cls = type(ev)
        code = RAW_TYPE_CODES.get(cls)
        if code is None:
            raise CodecError(f"unregistered raw event type {cls.__name__}")
        values = [getattr(ev, f) for f in _FIELDS[cls]]
        return self._frame(self._packer.pack([KIND_RAW, code, *values]))

    def gap(self, first_seq: int, last_seq: int, count: int, reason: str, mono_ns: int, wall_ns: int) -> bytes:
        return self._frame(self._packer.pack([KIND_GAP, first_seq, last_seq, count, reason, mono_ns, wall_ns]))

    def footer(self, info: dict) -> bytes:
        return self._frame(self._packer.pack([KIND_FOOTER, info]))


class Decoder:
    """Decodes one part file's records using that part's header type table."""

    def __init__(self) -> None:
        self._types: dict[int, tuple[type, tuple[str, ...]]] | None = None

    def load_header(self, body: dict) -> None:
        if body.get("format") != FORMAT_NAME:
            raise CodecError(f"not a Hermès recording (format={body.get('format')!r})")
        if body.get("schema_version") != SCHEMA_VERSION:
            raise CodecError(f"unsupported schema_version {body.get('schema_version')} (expected {SCHEMA_VERSION})")
        types = {}
        for code, (name, fields) in body["types"].items():
            cls = getattr(R, name, None)
            if cls is None or not dataclasses.is_dataclass(cls) or cls not in _FIELDS:
                raise CodecError(f"unknown raw type {name!r} in header")
            if set(fields) != set(_FIELDS[cls]):
                # Never guess: a field added/removed/renamed means a different schema.
                raise CodecError(f"field mismatch for {name}: recorded {list(fields)} vs current {list(_FIELDS[cls])}")
            types[int(code)] = (cls, tuple(fields))
        self._types = types

    def raw(self, rec: list) -> R.RawEvent:
        if self._types is None:
            raise CodecError("raw record before header")
        code = rec[1]
        try:
            cls, fields = self._types[code]
        except KeyError as exc:
            raise CodecError(f"unknown type code {code}") from exc
        values = rec[2:]
        if len(values) != len(fields):
            raise CodecError(f"{cls.__name__}: {len(values)} values for {len(fields)} fields")
        return cls(**{f: (_tuplify(v) if type(v) is list else v) for f, v in zip(fields, values)})


TAIL_TRUNCATED = "truncated_tail"   # last frame incomplete (crash mid-write): all earlier records intact
TAIL_CORRUPT = "corrupt"            # implausible length prefix or undecodable payload: data after it unreadable


def _unpack(b: bytes) -> Any:
    return msgpack.unpackb(b, ext_hook=_ext_hook, raw=False, strict_map_key=False)


def scan_frames(data: bytes, offset: int = 0) -> Iterator[tuple[int, list | None, bytes | str]]:
    """Yield ``(offset, record, payload_bytes)`` per complete frame. A readable prefix ends with one
    final ``(offset, None, reason)`` where reason is ``TAIL_TRUNCATED`` or ``TAIL_CORRUPT``.

    Truncated tail = fewer than 4 bytes left, or a plausible length prefix that runs past the end of
    the file (the writer died mid-frame). Corrupt = length prefix above ``MAX_RECORD_BYTES`` or a
    complete frame whose payload does not decode.
    """
    n = len(data)
    while offset < n:
        if n - offset < 4:
            yield offset, None, TAIL_TRUNCATED
            return
        (length,) = _LEN.unpack_from(data, offset)
        if length > MAX_RECORD_BYTES:
            yield offset, None, TAIL_CORRUPT
            return
        end = offset + 4 + length
        if end > n:
            yield offset, None, TAIL_TRUNCATED
            return
        payload = data[offset + 4: end]
        try:
            rec = _unpack(payload)
        except Exception:  # noqa: BLE001 - corrupt payload ends the readable prefix
            yield offset, None, TAIL_CORRUPT
            return
        if not isinstance(rec, list) or not rec or not isinstance(rec[0], int):
            yield offset, None, TAIL_CORRUPT
            return
        yield offset, rec, payload
        offset = end


def iter_frames(data: bytes, offset: int = 0) -> Iterator[tuple[int, list | None]]:
    """Yield (offset, record) for each complete frame; a final (offset, None) marks a truncated/corrupt tail."""
    for off, rec, _ in scan_frames(data, offset):
        yield off, rec
