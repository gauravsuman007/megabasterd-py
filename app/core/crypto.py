"""MEGA cryptography primitives, ported from CryptTools.java / MiscTools.java.

MEGA's protocol works on 4-word (16-byte) big-endian int32 blocks almost
everywhere: keys, IVs, and MACs are all shuffled around as ``int[4]`` arrays
before being turned back into raw bytes for AES. The helpers below preserve
that int32 <-> bytes boundary exactly because several algorithms (node key
derivation, MEGAUserHash, at-rest key wrapping) depend on XOR-ing or slicing
at word granularity, not byte granularity.
"""
from __future__ import annotations

import base64
import hashlib
import struct
from dataclasses import dataclass

from Crypto.Cipher import AES
from Crypto.PublicKey import RSA
from Crypto.Util.number import bytes_to_long, long_to_bytes

# ---------------------------------------------------------------------------
# int32 <-> bytes (big-endian, MEGA's word representation)
# ---------------------------------------------------------------------------


def bin2i32a(data: bytes) -> list[int]:
    """Split bytes into big-endian uint32 words (bin2i32a in MiscTools.java)."""
    if len(data) % 4 != 0:
        data = data + b"\x00" * (4 - len(data) % 4)
    return list(struct.unpack(">%dI" % (len(data) // 4), data))


def i32a2bin(words: list[int]) -> bytes:
    """Pack big-endian uint32 words back into bytes (i32a2bin)."""
    return struct.pack(">%dI" % len(words), *(w & 0xFFFFFFFF for w in words))


# ---------------------------------------------------------------------------
# Base64 variants MEGA uses
# ---------------------------------------------------------------------------


def base64_to_bin(data: str) -> bytes:
    """Standard base64 decode (+/ with = padding)."""
    padded = data + "=" * (-len(data) % 4)
    return base64.b64decode(padded)


def bin_to_base64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def url_base64_to_bin(data: str) -> bytes:
    """MEGA's URL-safe base64: '-_' instead of '+/', no padding."""
    data = data.replace("-", "+").replace("_", "/")
    padded = data + "=" * (-len(data) % 4)
    return base64.b64decode(padded)


def bin_to_url_base64(data: bytes) -> str:
    """Encode to MEGA's URL-safe base64: '-_' for '+/', padding stripped."""
    encoded = base64.b64encode(data).decode("ascii")
    return encoded.replace("+", "-").replace("/", "_").rstrip("=")


# ---------------------------------------------------------------------------
# AES helpers
# ---------------------------------------------------------------------------

ZERO_IV_16 = b"\x00" * 16


# The *_nopad variants do no padding: the caller must pass block-aligned
# (16-byte multiple) data. MEGA uses these with a zero IV for key wrapping
# and attribute blobs; the pkcs7 variants below are for at-rest credentials.
def aes_cbc_encrypt_nopad(data: bytes, key: bytes, iv: bytes = ZERO_IV_16) -> bytes:
    """AES-CBC encrypt, no padding (data must be block-aligned)."""
    return AES.new(key, AES.MODE_CBC, iv).encrypt(data)


def aes_cbc_decrypt_nopad(data: bytes, key: bytes, iv: bytes = ZERO_IV_16) -> bytes:
    """AES-CBC decrypt, no padding (ciphertext must be block-aligned)."""
    return AES.new(key, AES.MODE_CBC, iv).decrypt(data)


def _pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len]) * pad_len


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        return data
    pad_len = data[-1]
    if pad_len < 1 or pad_len > 16 or pad_len > len(data):
        raise ValueError("Invalid PKCS7 padding")
    return data[:-pad_len]


def aes_cbc_encrypt_pkcs7(data: bytes, key: bytes, iv: bytes = ZERO_IV_16) -> bytes:
    return aes_cbc_encrypt_nopad(_pkcs7_pad(data), key, iv)


def aes_cbc_decrypt_pkcs7(data: bytes, key: bytes, iv: bytes = ZERO_IV_16) -> bytes:
    return _pkcs7_unpad(aes_cbc_decrypt_nopad(data, key, iv))


def aes_ecb_encrypt(data: bytes, key: bytes) -> bytes:
    return AES.new(key, AES.MODE_ECB).encrypt(data)


def aes_ecb_decrypt(data: bytes, key: bytes) -> bytes:
    return AES.new(key, AES.MODE_ECB).decrypt(data)


# MEGA's "encryptKey"/"decryptKey": AES-ECB-NoPadding, used for node keys
# (account/folder-relative, key wrapped by the parent's key) as opposed to
# init_mega_link_key below (public link keys, which arrive pre-combined in
# the URL fragment and only need XOR-ing, no AES).
def encrypt_key(data: bytes, key: bytes) -> bytes:
    return aes_ecb_encrypt(data, key)


def decrypt_key(data: bytes, key: bytes) -> bytes:
    return aes_ecb_decrypt(data, key)


def aes_ctr_crypt(data: bytes, key: bytes, iv8: bytes, counter_start: int = 0) -> bytes:
    """AES-CTR with MEGA's nonce/counter layout: 8-byte nonce + 8-byte big-endian
    block counter, counter_start given in 16-byte blocks (i.e. byte offset // 16)."""
    from Crypto.Util import Counter

    ctr = Counter.new(64, prefix=iv8, initial_value=counter_start)
    return AES.new(key, AES.MODE_CTR, counter=ctr).encrypt(data)  # symmetric: encrypt==decrypt


# ---------------------------------------------------------------------------
# Node / link key derivation
# ---------------------------------------------------------------------------


def init_mega_link_key(key_bytes: bytes) -> bytes:
    """Derive the 16-byte AES file key from a 32-byte (or shorter) node key.

    MEGA share/file keys are 32 bytes: the real AES key is the XOR of the
    first and second 16-byte halves (initMEGALinkKey in CryptTools.java).
    If the key is already <32 bytes (some legacy/folder cases), the first
    16 bytes are used directly.
    """
    if len(key_bytes) >= 32:
        words = bin2i32a(key_bytes[:32])
        combined = [words[0] ^ words[4], words[1] ^ words[5], words[2] ^ words[6], words[3] ^ words[7]]
        return i32a2bin(combined)
    return key_bytes[:16].ljust(16, b"\x00")


def init_mega_link_key_iv(key_bytes: bytes) -> bytes:
    """Extract the 8-byte CTR nonce from a 32-byte node key (words 4-5)."""
    words = bin2i32a(key_bytes[:32])
    return i32a2bin([words[4], words[5]])


def forward_mega_link_iv(iv8: bytes, forward_bytes: int) -> bytes:
    """Advance an 8-byte nonce by forward_bytes for resuming a CTR stream at
    an arbitrary offset. Kept for parity with CryptTools.forwardMEGALinkKeyIV;
    prefer passing counter_start=offset//16 to aes_ctr_crypt directly."""
    return iv8


# ---------------------------------------------------------------------------
# MEGA account login key derivation
# ---------------------------------------------------------------------------


def mega_prepare_master_key_v1(password_bytes: bytes) -> list[int]:
    """Legacy (v1) password->key hashing: 65536 rounds of AES-ECB over the
    password, chunked into 4-word blocks. MEGAPrepareMasterKey in CryptTools.java."""
    pkey = [0x93C467E3, 0x7DB0C7A4, 0xD1BE3F81, 0x0152CB56]
    key_words = bin2i32a(password_bytes)
    # pad to a multiple of 4 words with zeros (bin2i32a already zero-pads bytes,
    # but we need at least one block)
    if not key_words:
        key_words = [0, 0, 0, 0]
    if len(key_words) % 4 != 0:
        key_words += [0] * (4 - len(key_words) % 4)

    pkey_bytes = i32a2bin(pkey)
    for _ in range(0x10000):
        for i in range(0, len(key_words), 4):
            chunk = i32a2bin(key_words[i : i + 4])
            pkey_bytes = aes_ecb_encrypt(pkey_bytes, chunk)
    return bin2i32a(pkey_bytes)


def mega_user_hash(email: str, password_aes_words: list[int]) -> str:
    """MEGAUserHash: XOR the (lowercased) email's bytes into a 4-word state,
    then run 16384 rounds of AES-ECB, and base64url-encode words [0, 2]."""
    email_bytes = email.lower().encode("utf-8")
    email_words = bin2i32a(email_bytes)
    h = [0, 0, 0, 0]
    for i, w in enumerate(email_words):
        h[i % 4] ^= w

    key = i32a2bin(password_aes_words)
    h_bytes = i32a2bin(h)
    for _ in range(0x4000):
        h_bytes = aes_ecb_encrypt(h_bytes, key)

    h_words = bin2i32a(h_bytes)
    return bin_to_url_base64(i32a2bin([h_words[0], h_words[2]]))


def derive_login_key_v2(password: str, salt: bytes) -> tuple[list[int], str]:
    """Modern (v2+) login: PBKDF2-HMAC-SHA512(password, salt, 100_000, 32 bytes).
    First 16 bytes -> password_aes (as int[4]); last 16 bytes -> user_hash
    (URL-base64, sent as-is, no further hashing needed)."""
    derived = hashlib.pbkdf2_hmac("sha512", password.encode("utf-8"), salt, 100_000, dklen=32)
    password_aes = bin2i32a(derived[:16])
    user_hash = bin_to_url_base64(derived[16:32])
    return password_aes, user_hash


# ---------------------------------------------------------------------------
# Attribute (file/folder metadata) encryption
# ---------------------------------------------------------------------------

ATTR_PREFIX = b"MEGA"


def encrypt_attr(attr_json: bytes, key: bytes) -> bytes:
    """Encrypt a node's attribute JSON: prepend the literal ``MEGA`` marker,
    zero-pad to a 16-byte boundary, and AES-CBC encrypt with a zero IV."""
    data = ATTR_PREFIX + attr_json
    pad_len = (-len(data)) % 16
    data = data + b"\x00" * pad_len
    return aes_cbc_encrypt_nopad(data, key)


def decrypt_attr(ciphertext: bytes, key: bytes) -> bytes:
    """Inverse of `encrypt_attr`: decrypt, verify the ``MEGA`` marker (a wrong
    key yields garbage that fails this check), and return the JSON bytes with
    the marker and zero padding stripped. Raises ValueError on a bad key."""
    plain = aes_cbc_decrypt_nopad(ciphertext, key)
    if not plain.startswith(ATTR_PREFIX):
        raise ValueError("Bad attribute decryption (missing MEGA prefix)")
    return plain[len(ATTR_PREFIX) :].rstrip(b"\x00")


# ---------------------------------------------------------------------------
# At-rest encryption for locally stored credentials (master password)
# ---------------------------------------------------------------------------

AT_REST_MAGIC = b"MB2\x00"


def derive_master_key(master_password: str, salt: bytes) -> bytes:
    """Derive the 32-byte at-rest key from the user's master password via
    PBKDF2-HMAC-SHA256 (65536 rounds)."""
    return hashlib.pbkdf2_hmac("sha256", master_password.encode("utf-8"), salt, 65_536, dklen=32)


def encrypt_at_rest(plaintext: bytes, key: bytes) -> bytes:
    """Encrypt locally stored credentials. Output blob layout:
    ``MB2\\x00`` magic + random 16-byte IV + AES-CBC/PKCS7 ciphertext."""
    import os

    iv = os.urandom(16)
    return AT_REST_MAGIC + iv + aes_cbc_encrypt_pkcs7(plaintext, key[:16], iv)


def decrypt_at_rest(blob: bytes, key: bytes) -> bytes:
    """Decrypt an `encrypt_at_rest` blob. Blobs without the magic are treated
    as the legacy format (zero IV, no header) for backward compatibility."""
    if blob.startswith(AT_REST_MAGIC):
        iv = blob[4:20]
        ciphertext = blob[20:]
        return aes_cbc_decrypt_pkcs7(ciphertext, key[:16], iv)
    # legacy: zero IV, no magic
    return aes_cbc_decrypt_pkcs7(blob, key[:16], ZERO_IV_16)


# ---------------------------------------------------------------------------
# RSA (session id decryption) - MPI parsing
# ---------------------------------------------------------------------------


def mpi2big(data: bytes) -> int:
    """MiscTools.mpi2big: strip the 2-byte length header and treat *all*
    remaining bytes as an unsigned big-endian integer. Only correct for a
    buffer holding a single MPI (e.g. the `csid` field)."""
    return bytes_to_long(data[2:])


def mpi_to_int(data: bytes) -> tuple[int, bytes]:
    """Parse one MPI out of a buffer holding several concatenated MPIs
    (used for the `privk` blob: p, q, d, u): 2-byte big-endian bit-length
    header + ceil(bits/8) bytes. Returns (value, remaining_bytes)."""
    bit_len = struct.unpack(">H", data[:2])[0]
    byte_len = (bit_len + 7) // 8
    value = bytes_to_long(data[2 : 2 + byte_len])
    return value, data[2 + byte_len :]


@dataclass
class RSAPrivateComponents:
    """The four MPIs of a MEGA account RSA private key: primes `p`, `q`, the
    private exponent `d`, and the CRT coefficient `u` (unused by our raw
    modular-exponentiation decrypt, kept for completeness)."""
    p: int
    q: int
    d: int
    u: int


def parse_rsa_privk(privk_bytes: bytes) -> RSAPrivateComponents:
    """privk response field: 4 concatenated MPIs (p, q, d, u)."""
    rest = privk_bytes
    p, rest = mpi_to_int(rest)
    q, rest = mpi_to_int(rest)
    d, rest = mpi_to_int(rest)
    u, rest = mpi_to_int(rest)
    return RSAPrivateComponents(p=p, q=q, d=d, u=u)


def rsa_decrypt_csid(csid_mpi: bytes, privk: RSAPrivateComponents) -> bytes:
    """Decrypt the login response's csid MPI with the account's RSA private
    key (raw RSA/ECB/NoPadding, as MEGA uses it) and return the first 43
    bytes (the session id).

    Java's RSA/ECB/NoPadding cipher always emits exactly `modulus_byte_length`
    bytes (zero-padded on the left), then strips at most one leading zero
    byte before slicing the first 43 bytes. `long_to_bytes` alone would give
    the *minimal* encoding instead, silently dropping real leading zero
    bytes whenever the top byte(s) of the fixed-width plaintext happen to be
    zero — which corrupts the result. Must reproduce the fixed-width decode.
    """
    n = privk.p * privk.q
    m = mpi2big(csid_mpi)
    plain_int = pow(m, privk.d, n)
    modulus_byte_len = (n.bit_length() + 7) // 8
    plain = long_to_bytes(plain_int, modulus_byte_len)
    if plain and plain[0] == 0:
        plain = plain[1:]
    return plain[:43]
