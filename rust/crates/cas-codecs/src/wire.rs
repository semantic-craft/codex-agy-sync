//! Strict 64-bit Protobuf wire primitives matching `schema/antigravity_2_30.proto` and `_wire.py`.

use std::collections::BTreeMap;

pub use cas_core::UnknownWireField;
use cas_core::{format_timestamp_seconds_nanos, parse_iso_to_seconds_nanos};

use crate::error::CodecError;

/// Parsed wire value stored per field number.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WireValue {
    Varint(u64),
    Bytes(Vec<u8>),
}

/// Decoded wire field tuple `(field_number, wire_type, int_val_or_len, raw_bytes)`.
pub type WireField = (u32, u8, u64, Vec<u8>);

/// Encode an unsigned 64-bit integer as a protobuf varint.
#[must_use]
pub fn encode_varint(mut value: u64) -> Vec<u8> {
    let mut out = Vec::with_capacity(10);
    while value >= 0x80 {
        out.push(((value & 0x7F) as u8) | 0x80);
        value >>= 7;
    }
    out.push((value & 0x7F) as u8);
    out
}

/// Read an unsigned 64-bit varint from `buf[pos..]`, rejecting truncation and `>=64`-bit shifts.
///
/// # Errors
/// Returns [`CodecError::Wire`] on truncation or overflow past 64 bits.
pub fn read_varint(buf: &[u8], mut pos: usize) -> Result<(u64, usize), CodecError> {
    if pos >= buf.len() {
        return Err(CodecError::Wire("Truncated protobuf varint.".to_owned()));
    }
    let mut result: u64 = 0;
    let mut shift: u32 = 0;
    while pos < buf.len() {
        let b = buf[pos];
        pos += 1;
        if shift >= 64 || (shift == 63 && b > 1) {
            return Err(CodecError::Wire(
                "Protobuf varint exceeds 64 bits.".to_owned(),
            ));
        }
        result |= u64::from(b & 0x7F) << shift;
        if (b & 0x80) == 0 {
            return Ok((result, pos));
        }
        shift += 7;
    }
    Err(CodecError::Wire("Truncated protobuf varint.".to_owned()))
}

#[must_use]
pub const fn signed_int64(val: u64) -> i64 {
    val as i64
}

#[must_use]
pub const fn signed_int32(val: u64) -> i32 {
    (val as u32) as i32
}

/// Encode a protobuf field tag `(field_number << 3) | wire_type`.
///
/// # Errors
/// Returns [`CodecError::Wire`] if `field_number == 0` or `wire_type` is unsupported.
pub fn encode_tag(field_number: u32, wire_type: u8) -> Result<Vec<u8>, CodecError> {
    if field_number == 0 {
        return Err(CodecError::Wire(
            "Protobuf field_number must be >= 1.".to_owned(),
        ));
    }
    if !matches!(wire_type, 0 | 1 | 2 | 5) {
        return Err(CodecError::Wire(format!(
            "Unsupported protobuf wire_type {wire_type}."
        )));
    }
    Ok(encode_varint(
        (u64::from(field_number) << 3) | u64::from(wire_type),
    ))
}

#[must_use]
pub fn encode_varint_field(field_number: u32, value: i64) -> Vec<u8> {
    let mut out = encode_varint(u64::from(field_number) << 3);
    out.extend_from_slice(&encode_varint(value as u64));
    out
}

#[must_use]
pub fn encode_uvarint_field(field_number: u32, value: u64) -> Vec<u8> {
    let mut out = encode_varint(u64::from(field_number) << 3);
    out.extend_from_slice(&encode_varint(value));
    out
}

#[must_use]
pub fn encode_bytes_field(field_number: u32, data: &[u8]) -> Vec<u8> {
    let mut out = encode_varint((u64::from(field_number) << 3) | 2);
    out.extend_from_slice(&encode_varint(data.len() as u64));
    out.extend_from_slice(data);
    out
}

#[must_use]
pub fn encode_string_field(field_number: u32, text: &str) -> Vec<u8> {
    if text.is_empty() {
        Vec::new()
    } else {
        encode_bytes_field(field_number, text.as_bytes())
    }
}

/// Encode a single preserved [`UnknownWireField`].
///
/// # Errors
/// Returns [`CodecError::Wire`] if the stored wire type or fixed byte length is invalid.
pub fn encode_unknown_field(uf: &UnknownWireField) -> Result<Vec<u8>, CodecError> {
    match uf.wire_type {
        0 => {
            let mut out = encode_tag(uf.field_tag, 0)?;
            if uf.raw_bytes.is_empty() {
                out.push(0);
            } else {
                let _ = read_varint(&uf.raw_bytes, 0)?;
                out.extend_from_slice(&uf.raw_bytes);
            }
            Ok(out)
        }
        1 => {
            if uf.raw_bytes.len() != 8 {
                return Err(CodecError::Wire(format!(
                    "fixed64 field requires 8 bytes, got {}.",
                    uf.raw_bytes.len()
                )));
            }
            let mut out = encode_tag(uf.field_tag, 1)?;
            out.extend_from_slice(&uf.raw_bytes);
            Ok(out)
        }
        2 => {
            let mut out = encode_tag(uf.field_tag, 2)?;
            out.extend_from_slice(&encode_varint(uf.raw_bytes.len() as u64));
            out.extend_from_slice(&uf.raw_bytes);
            Ok(out)
        }
        5 => {
            if uf.raw_bytes.len() != 4 {
                return Err(CodecError::Wire(format!(
                    "fixed32 field requires 4 bytes, got {}.",
                    uf.raw_bytes.len()
                )));
            }
            let mut out = encode_tag(uf.field_tag, 5)?;
            out.extend_from_slice(&uf.raw_bytes);
            Ok(out)
        }
        other => Err(CodecError::Wire(format!(
            "Unsupported wire_type {other} in UnknownWireField."
        ))),
    }
}

#[must_use]
pub fn encode_unknown_fields(fields: &[UnknownWireField]) -> Vec<u8> {
    let mut out = Vec::new();
    for uf in fields {
        if let Ok(encoded) = encode_unknown_field(uf) {
            out.extend_from_slice(&encoded);
        }
    }
    out
}

/// Decode `buf` into `(field_number, wire_type, int_val_or_len, raw_bytes)` entries.
///
/// # Errors
/// Returns [`CodecError::Wire`] on truncated or malformed protobuf wire bytes.
pub fn iter_wire_fields(buf: &[u8]) -> Result<Vec<WireField>, CodecError> {
    let mut pos = 0;
    let n = buf.len();
    let mut fields = Vec::new();
    while pos < n {
        let tag_start = pos;
        let (tag, next_pos) = read_varint(buf, pos)?;
        pos = next_pos;
        let fn_num = (tag >> 3) as u32;
        let wt = (tag & 0x07) as u8;
        if fn_num == 0 {
            return Err(CodecError::Wire(format!(
                "Invalid protobuf field number 0 at offset {tag_start}."
            )));
        }
        match wt {
            0 => {
                let val_start = pos;
                let (ival, after) = read_varint(buf, pos)?;
                pos = after;
                fields.push((fn_num, 0, ival, buf[val_start..pos].to_vec()));
            }
            1 | 5 => {
                let width = if wt == 1 { 8 } else { 4 };
                if pos + width > n {
                    return Err(CodecError::Wire(format!(
                        "Truncated fixed{} protobuf field.",
                        width * 8
                    )));
                }
                let raw = &buf[pos..pos + width];
                let mut arr = [0_u8; 8];
                arr[..width].copy_from_slice(raw);
                let ival = u64::from_le_bytes(arr);
                fields.push((fn_num, wt, ival, raw.to_vec()));
                pos += width;
            }
            2 => {
                let (ln_u64, after) = read_varint(buf, pos)?;
                pos = after;
                let ln = usize::try_from(ln_u64).map_err(|_| {
                    CodecError::Wire("Length-delimited field length overflow.".to_owned())
                })?;
                if pos.saturating_add(ln) > n {
                    return Err(CodecError::Wire(
                        "Truncated length-delimited protobuf field.".to_owned(),
                    ));
                }
                fields.push((fn_num, 2, ln_u64, buf[pos..pos + ln].to_vec()));
                pos += ln;
            }
            other => {
                return Err(CodecError::Wire(format!(
                    "Unsupported protobuf wire type {other} at offset {tag_start}."
                )));
            }
        }
    }
    Ok(fields)
}

/// Map of `field_number -> Vec<(wire_type, WireValue)>`.
pub type FieldMap = BTreeMap<u32, Vec<(u8, WireValue)>>;

/// Parse `buf` into a [`FieldMap`] and optional [`UnknownWireField`] list when `known_tags` is given.
///
/// # Errors
/// Propagates [`CodecError::Wire`] on malformed input.
pub fn parse_proto_fields(
    buf: &[u8],
    known_tags: Option<&[u32]>,
) -> Result<(FieldMap, Vec<UnknownWireField>), CodecError> {
    let mut field_map: FieldMap = BTreeMap::new();
    let mut unknowns = Vec::new();
    for (fn_num, wt, ival, raw) in iter_wire_fields(buf)? {
        let is_known = known_tags.is_none_or(|tags| tags.contains(&fn_num));
        if !is_known {
            unknowns.push(UnknownWireField {
                field_tag: fn_num,
                wire_type: wt,
                raw_bytes: raw.clone(),
            });
        }
        let val = if wt == 0 {
            WireValue::Varint(ival)
        } else {
            WireValue::Bytes(raw)
        };
        field_map.entry(fn_num).or_default().push((wt, val));
    }
    Ok((field_map, unknowns))
}

#[must_use]
pub fn encode_timestamp_proto(iso_str: &str) -> Vec<u8> {
    if iso_str.trim().is_empty() {
        return Vec::new();
    }
    let (seconds, nanos) = parse_iso_to_seconds_nanos(iso_str);
    let mut out = Vec::new();
    if seconds != 0 {
        out.extend_from_slice(&encode_varint_field(1, seconds));
    }
    if nanos != 0 {
        out.extend_from_slice(&encode_varint_field(2, i64::from(nanos)));
    }
    out
}

/// Decode a `google.protobuf.Timestamp` wire payload into ISO-8601 UTC.
///
/// # Errors
/// Propagates [`CodecError::Wire`] on malformed wire bytes.
pub fn decode_timestamp_proto(raw: &[u8], default_iso: &str) -> Result<String, CodecError> {
    let mut seconds: i64 = 0;
    let mut nanos: i32 = 0;
    for (fn_num, wt, ival, _) in iter_wire_fields(raw)? {
        if fn_num == 1 && wt == 0 {
            seconds = signed_int64(ival);
        } else if fn_num == 2 && wt == 0 {
            nanos = signed_int32(ival);
        }
    }
    Ok(format_timestamp_seconds_nanos(seconds, nanos, default_iso))
}

/// Helper: extract the last UTF-8 string value for `tag`.
#[must_use]
pub fn fm_last_str(fm: &FieldMap, tag: u32) -> String {
    let mut out = String::new();
    if let Some(entries) = fm.get(&tag) {
        for (wt, val) in entries {
            if *wt == 2 {
                if let WireValue::Bytes(raw) = val {
                    out = String::from_utf8_lossy(raw).into_owned();
                }
            }
        }
    }
    out
}

#[must_use]
pub fn fm_last_str_fallback(fm: &FieldMap, tag: u32, fallback: &str) -> String {
    let mut out = fallback.to_owned();
    if let Some(entries) = fm.get(&tag) {
        for (wt, val) in entries {
            if *wt == 2 {
                if let WireValue::Bytes(raw) = val {
                    let s = String::from_utf8_lossy(raw).into_owned();
                    if !s.is_empty() || fallback.is_empty() {
                        out = s;
                    }
                }
            }
        }
    }
    out
}

#[must_use]
pub fn fm_opt_str(fm: &FieldMap, tag: u32, fallback: Option<&str>) -> Option<String> {
    let mut out = fallback.filter(|s| !s.is_empty()).map(str::to_owned);
    if let Some(entries) = fm.get(&tag) {
        for (wt, val) in entries {
            if *wt == 2 {
                if let WireValue::Bytes(raw) = val {
                    let s = String::from_utf8_lossy(raw).into_owned();
                    out = if s.is_empty() { None } else { Some(s) };
                }
            }
        }
    }
    out
}

#[must_use]
pub fn fm_strs(fm: &FieldMap, tag: u32) -> Vec<String> {
    let mut out = Vec::new();
    if let Some(entries) = fm.get(&tag) {
        for (wt, val) in entries {
            if *wt == 2 {
                if let WireValue::Bytes(raw) = val {
                    out.push(String::from_utf8_lossy(raw).into_owned());
                }
            }
        }
    }
    out
}

#[must_use]
pub fn fm_bytes(fm: &FieldMap, tag: u32) -> Vec<&[u8]> {
    let mut out = Vec::new();
    if let Some(entries) = fm.get(&tag) {
        for (wt, val) in entries {
            if *wt == 2 {
                if let WireValue::Bytes(raw) = val {
                    out.push(raw.as_slice());
                }
            }
        }
    }
    out
}

#[must_use]
pub fn fm_last_varint(fm: &FieldMap, tag: u32) -> Option<u64> {
    let mut out = None;
    if let Some(entries) = fm.get(&tag) {
        for (wt, val) in entries {
            if *wt == 0 {
                if let WireValue::Varint(v) = val {
                    out = Some(*v);
                }
            }
        }
    }
    out
}
