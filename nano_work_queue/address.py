"""Nano account address validation.

A Nano address is `nano_` followed by 60 characters of Nano's own base32
alphabet: 52 characters encoding the 256-bit public key (with four bits of
leading padding) and 8 characters encoding a 5-byte blake2b checksum of
that key, stored little-endian.

Validating the checksum is the whole point of this module. A single
mistyped character produces a well-formed address that belongs to nobody,
and a payout sent there is gone. `blake2b` is in the standard library, so
this needs no dependency.
"""

import hashlib

ALPHABET = "13456789abcdefghijkmnopqrstuwxyz"
_DECODE = {ch: i for i, ch in enumerate(ALPHABET)}

PREFIXES = ("nano_", "xrb_")
BODY_LEN = 60
KEY_CHARS = 52
CHECKSUM_CHARS = 8


class InvalidAddress(ValueError):
    """The address is not a Nano address, or its checksum does not match."""


def _b32_to_bits(text: str) -> str:
    out = []
    for ch in text:
        try:
            out.append(f"{_DECODE[ch]:05b}")
        except KeyError:
            raise InvalidAddress(
                f"character {ch!r} is not in the Nano base32 alphabet"
            ) from None
    return "".join(out)


def normalise(address) -> str:
    """Return the address in its canonical `nano_` form, or raise.

    Canonical means: lower-cased, `xrb_` rewritten to `nano_`, surrounding
    whitespace removed. The checksum is verified before anything is
    returned, so a normalised address is always a payable one.
    """
    if not isinstance(address, str):
        raise InvalidAddress("address must be a string")
    s = address.strip().lower()
    for prefix in PREFIXES:
        if s.startswith(prefix):
            body = s[len(prefix) :]
            break
    else:
        raise InvalidAddress("address must start with 'nano_' or 'xrb_'")

    if len(body) != BODY_LEN:
        raise InvalidAddress(
            f"address body must be {BODY_LEN} characters, got {len(body)}"
        )
    if body[0] not in "13":
        # The first character encodes the four padding bits plus the top bit
        # of the key, so only '1' and '3' can ever appear there.
        raise InvalidAddress("address body must begin with '1' or '3'")

    key_bits = _b32_to_bits(body[:KEY_CHARS])[4:]          # drop the padding
    checksum_bits = _b32_to_bits(body[KEY_CHARS:])
    public_key = int(key_bits, 2).to_bytes(32, "big")
    declared = int(checksum_bits, 2).to_bytes(5, "big")

    expected = hashlib.blake2b(public_key, digest_size=5).digest()[::-1]
    if declared != expected:
        raise InvalidAddress("address checksum does not match")

    return "nano_" + body


def is_valid(address) -> bool:
    try:
        normalise(address)
    except InvalidAddress:
        return False
    return True
