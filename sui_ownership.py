"""Strict Move type and Kiosk authority checks shared by NFT readers."""

import re

from verification_security import canonical_sui_address


# Mysten's mainnet deployment, published in @mysten/kiosk constants.ts.
# Additional deployments must be explicitly reviewed and configured by operators.
MAINNET_PERSONAL_CAP = (
    "0x0cb4bcc0560340eb1a1b929cabe56b33fc6449820ec8c1980d69bb98b649b802"
    "::personal_kiosk::PersonalKioskCap"
)
_TOKEN = re.compile(r"0[xX][0-9a-fA-F]+|[a-zA-Z_][a-zA-Z_0-9]*|::|[<>,]")
_IDENTIFIER = re.compile(r"[a-zA-Z_][a-zA-Z_0-9]*\Z")
_PRIMITIVES = {"bool", "u8", "u16", "u32", "u64", "u128", "u256", "address", "signer"}


def canonical_move_type(value):
    """Parse a complete type, preserving case and normalizing every address."""
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("Invalid Move type")
    compact = re.sub(r"\s+", "", value)
    tokens = _TOKEN.findall(compact)
    if "".join(tokens) != compact:
        raise ValueError("Invalid Move type")
    index = 0

    def take():
        nonlocal index
        if index >= len(tokens):
            raise ValueError("Incomplete Move type")
        token = tokens[index]
        index += 1
        return token

    def parse(depth=0):
        nonlocal index
        if depth > 16:
            raise ValueError("Move type is too deeply nested")
        first = take()
        if first in _PRIMITIVES:
            return first
        if first == "vector":
            if take() != "<":
                raise ValueError("Invalid vector type")
            result = "vector<" + parse(depth + 1)
            if take() != ">":
                raise ValueError("Invalid vector type")
            return result + ">"
        address = canonical_sui_address(first)
        if not address or take() != "::":
            raise ValueError("Expected a package::module::Type")
        module = take()
        if take() != "::":
            raise ValueError("Expected a package::module::Type")
        name = take()
        if not _IDENTIFIER.fullmatch(module) or not _IDENTIFIER.fullmatch(name):
            raise ValueError("Invalid Move identifier")
        result = f"{address}::{module}::{name}"
        if index < len(tokens) and tokens[index] == "<":
            index += 1
            args = [parse(depth + 1)]
            while index < len(tokens) and tokens[index] == ",":
                index += 1
                args.append(parse(depth + 1))
            if take() != ">":
                raise ValueError("Unclosed type arguments")
            result += "<" + ",".join(args) + ">"
        return result

    result = parse()
    if index != len(tokens):
        raise ValueError("Trailing Move type input")
    return result


def normalize_collection(value):
    value = (value or "").strip()
    if not value:
        return ""
    if "::" not in value:
        address = canonical_sui_address(value)
        if not address:
            raise ValueError("Use a package address or a full package::module::Type")
        return address
    result = canonical_move_type(value)
    if not result.startswith("0x"):
        raise ValueError("A collection must be a struct type")
    return result


def collection_matches(object_type, collection):
    """Match the outer type/package, never nested arguments or substrings."""
    normalized = normalize_collection(collection)
    try:
        candidate = canonical_move_type(object_type)
    except ValueError:
        return False
    if candidate.split("<", 1)[0] == canonical_move_type("0x2::coin::Coin"):
        return False
    if "::" in normalized:
        return candidate == normalized
    return bool(normalized and candidate.split("::", 1)[0] == normalized)


def personal_cap_types(extra_types=""):
    types = {canonical_move_type(MAINNET_PERSONAL_CAP)}
    for value in extra_types.split(","):
        if value.strip():
            normalized = canonical_move_type(value.strip())
            if not normalized.endswith("::personal_kiosk::PersonalKioskCap"):
                raise ValueError("Unsupported personal Kiosk capability type")
            types.add(normalized)
    return frozenset(types)


def trusted_personal_cap(object_type, allowed_types):
    try:
        return canonical_move_type(object_type) in allowed_types
    except ValueError:
        return False
