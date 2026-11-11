"""Strict Protobuf wire primitives shared across codecs, projections, and schema scripts."""

from __future__ import annotations

from collections.abc import Container, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re

_EPOCH_UTC = datetime(1970, 1, 1, tzinfo=timezone.utc)
_ISO_FRAC_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})(?:\.(\d+))?(.*)$")
_UINT64_LIMIT = 1 << 64
_INT64_MIN = -(1 << 63)
_VALID_WIRE_TYPES = frozenset({0, 1, 2, 5})


@dataclass(frozen=True)
class UnknownWireField:
    """Opaque protobuf wire field preserved for forward-compatible round-tripping."""

    field_tag: int
    wire_type: int
    raw_bytes: bytes


def encode_varint(value: int) -> bytes:
    """Encode an unsigned 64-bit integer (`0 <= value < 2**64`) as a protobuf varint."""
    if isinstance(value, bool):
        value = int(value)
    if value < 0 or value >= _UINT64_LIMIT:
        raise ValueError(f"Unsigned protobuf varint out of range [0, 2^64): {value}")
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value & 0x7F)
    return bytes(out)


def read_varint(buf: bytes, pos: int = 0) -> tuple[int, int]:
    """Read an unsigned 64-bit varint from `buf[pos:]`, rejecting truncation and >=64-bit shifts."""
    n = len(buf)
    if pos < 0 or pos >= n:
        raise ValueError("Truncated protobuf varint.")
    result = 0
    shift = 0
    while pos < n:
        b = buf[pos]
        pos += 1
        if shift >= 64 or (shift == 63 and b > 1):
            raise ValueError("Protobuf varint exceeds 64 bits.")
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
    raise ValueError("Truncated protobuf varint.")


def signed_int64(val: int) -> int:
    return val - _UINT64_LIMIT if val >= (1 << 63) else val


def signed_int32(val: int) -> int:
    masked = val & 0xFFFFFFFF
    return masked - (1 << 32) if masked >= (1 << 31) else masked


def encode_tag(field_number: int, wire_type: int) -> bytes:
    if field_number <= 0:
        raise ValueError(f"Protobuf field_number must be >= 1, got {field_number}.")
    if wire_type not in _VALID_WIRE_TYPES:
        raise ValueError(f"Unsupported protobuf wire_type {wire_type}.")
    return encode_varint((field_number << 3) | wire_type)


def encode_varint_field(field_number: int, value: int) -> bytes:
    if isinstance(value, bool):
        value = int(value)
    if value < _INT64_MIN:
        raise ValueError(f"Signed protobuf varint below int64 min: {value}")
    unsigned = value + _UINT64_LIMIT if value < 0 else value
    return encode_tag(field_number, 0) + encode_varint(unsigned)


def encode_bytes_field(field_number: int, data: bytes) -> bytes:
    payload = bytes(data)
    return encode_tag(field_number, 2) + encode_varint(len(payload)) + payload


def encode_string_field(field_number: int, text: str) -> bytes:
    return encode_bytes_field(field_number, text.encode("utf-8")) if text else b""


def encode_fixed64_field(field_number: int, raw8: bytes) -> bytes:
    payload = bytes(raw8)
    if len(payload) != 8:
        raise ValueError(f"fixed64 field requires 8 bytes, got {len(payload)}.")
    return encode_tag(field_number, 1) + payload


def encode_fixed32_field(field_number: int, raw4: bytes) -> bytes:
    payload = bytes(raw4)
    if len(payload) != 4:
        raise ValueError(f"fixed32 field requires 4 bytes, got {len(payload)}.")
    return encode_tag(field_number, 5) + payload


def encode_unknown_field(uf: UnknownWireField) -> bytes:
    if uf.wire_type == 0:
        if uf.raw_bytes:
            read_varint(uf.raw_bytes, 0)
            return encode_tag(uf.field_tag, 0) + bytes(uf.raw_bytes)
        return encode_tag(uf.field_tag, 0) + b"\x00"
    if uf.wire_type == 1:
        return encode_fixed64_field(uf.field_tag, uf.raw_bytes)
    if uf.wire_type == 2:
        return encode_bytes_field(uf.field_tag, uf.raw_bytes)
    if uf.wire_type == 5:
        return encode_fixed32_field(uf.field_tag, uf.raw_bytes)
    raise ValueError(f"Unsupported wire_type {uf.wire_type} in UnknownWireField.")


def encode_unknown_fields(fields: Iterable[UnknownWireField]) -> bytes:
    return b"".join(encode_unknown_field(uf) for uf in fields)


def iter_wire_fields(buf: bytes) -> list[tuple[int, int, int, bytes]]:
    """Decode `buf` into `(field_number, wire_type, int_val_or_len, raw_bytes)` tuples."""
    pos = 0
    n = len(buf)
    fields: list[tuple[int, int, int, bytes]] = []
    while pos < n:
        tag_start = pos
        tag, pos = read_varint(buf, pos)
        fn, wt = tag >> 3, tag & 0x07
        if fn <= 0:
            raise ValueError(f"Invalid protobuf field number {fn} at offset {tag_start}.")
        if wt == 0:
            val_start = pos
            ival, pos = read_varint(buf, pos)
            fields.append((fn, 0, ival, bytes(buf[val_start:pos])))
        elif wt in (1, 5):
            width = 8 if wt == 1 else 4
            if pos + width > n:
                raise ValueError(f"Truncated fixed{width * 8} protobuf field.")
            raw = bytes(buf[pos : pos + width])
            fields.append((fn, wt, int.from_bytes(raw, "little"), raw))
            pos += width
        elif wt == 2:
            ln, pos = read_varint(buf, pos)
            if pos + ln > n:
                raise ValueError("Truncated length-delimited protobuf field.")
            fields.append((fn, 2, ln, bytes(buf[pos : pos + ln])))
            pos += ln
        else:
            raise ValueError(f"Unsupported protobuf wire type {wt} at offset {tag_start}.")
    return fields


def parse_proto_fields(
    buf: bytes,
    known_tags: Container[int] | None = None,
) -> tuple[dict[int, list[tuple[int, int | bytes]]], list[UnknownWireField]]:
    """Parse `buf` into `{tag: [(wire_type, int | bytes), ...]}` and preserved unknown fields."""
    field_map: dict[int, list[tuple[int, int | bytes]]] = {}
    unknowns: list[UnknownWireField] = []
    for fn, wt, ival, raw in iter_wire_fields(buf):
        field_map.setdefault(fn, []).append((wt, ival if wt == 0 else raw))
        if known_tags is None or fn not in known_tags:
            unknowns.append(UnknownWireField(field_tag=fn, wire_type=wt, raw_bytes=raw))
    return field_map, unknowns


def parse_iso_to_seconds_nanos(iso_str: str) -> tuple[int, int]:
    """Convert an ISO-8601 timestamp string into `(unix_seconds, nanos)`."""
    cleaned = (iso_str or "").strip()
    if not cleaned:
        return (1700000000, 0)
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        m = _ISO_FRAC_RE.match(cleaned)
        nanos_override = int((m.group(2) or "").ljust(9, "0")[:9]) if m else None
        dt_str = f"{m.group(1)}{m.group(3)}" if m else cleaned
        dt = datetime.fromisoformat(dt_str)
        utc_dt = (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt).astimezone(timezone.utc)
        seconds = (utc_dt - _EPOCH_UTC) // timedelta(seconds=1)
        return (seconds, nanos_override if nanos_override is not None else utc_dt.microsecond * 1000)
    except ValueError:
        return (1700000000, 0)


def encode_timestamp_proto(iso_str: str) -> bytes:
    if not (iso_str or "").strip():
        return b""
    seconds, nanos = parse_iso_to_seconds_nanos(iso_str)
    return (encode_varint_field(1, seconds) if seconds else b"") + (
        encode_varint_field(2, nanos) if nanos else b""
    )


def decode_timestamp_proto(raw: bytes, default_iso: str = "2026-01-01T00:00:00Z") -> str:
    seconds = nanos = 0
    for fn, wt, ival, _ in iter_wire_fields(raw):
        if fn == 1 and wt == 0:
            seconds = signed_int64(ival)
        elif fn == 2 and wt == 0:
            nanos = signed_int32(ival)
    if seconds <= 0:
        return default_iso
    base = (_EPOCH_UTC + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S")
    if nanos <= 0:
        return f"{base}Z"
    frac = f"{nanos:09d}".rstrip("0") if nanos % 1000 else f"{nanos // 1000:06d}"
    return f"{base}.{frac}Z"

