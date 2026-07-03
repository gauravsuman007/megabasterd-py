import json

import pytest

from app.core import crypto, hashcash
from app.core.link_parser import new_links_to_legacy, parse_mega_link


def test_i32a_roundtrip():
    data = bytes(range(16))
    words = crypto.bin2i32a(data)
    assert len(words) == 4
    assert crypto.i32a2bin(words) == data


def test_base64_roundtrip():
    data = b"\x00\x01\xff\xfe\x10hello world"
    assert crypto.base64_to_bin(crypto.bin_to_base64(data)) == data
    assert crypto.url_base64_to_bin(crypto.bin_to_url_base64(data)) == data


def test_url_base64_no_padding_and_url_safe_chars():
    # bytes chosen so standard base64 would contain '+' '/' and need '=' padding
    data = bytes([0xFB, 0xFF, 0xBF])
    std = crypto.bin_to_base64(data)
    assert "+" in std or "/" in std
    url = crypto.bin_to_url_base64(data)
    assert "+" not in url and "/" not in url and "=" not in url
    assert crypto.url_base64_to_bin(url) == data


def test_aes_ecb_roundtrip():
    key = bytes(range(16))
    data = bytes(range(16, 32))
    enc = crypto.aes_ecb_encrypt(data, key)
    assert crypto.aes_ecb_decrypt(enc, key) == data


def test_aes_cbc_nopad_roundtrip():
    key = bytes(range(16))
    iv = bytes(reversed(range(16)))
    data = bytes(range(32))
    enc = crypto.aes_cbc_encrypt_nopad(data, key, iv)
    assert crypto.aes_cbc_decrypt_nopad(enc, key, iv) == data


def test_aes_cbc_pkcs7_roundtrip():
    key = bytes(range(16))
    data = b"not a multiple of 16 bytes!"
    enc = crypto.aes_cbc_encrypt_pkcs7(data, key)
    assert crypto.aes_cbc_decrypt_pkcs7(enc, key) == data


def test_aes_ctr_roundtrip_and_offset_resume():
    key = bytes(range(16))
    nonce = bytes(range(8))
    plaintext = bytes((i % 251) for i in range(1024))

    # Encrypt in one shot from offset 0.
    ciphertext_full = crypto.aes_ctr_crypt(plaintext, key, nonce, counter_start=0)
    assert crypto.aes_ctr_crypt(ciphertext_full, key, nonce, counter_start=0) == plaintext

    # Encrypt split at a 16-byte-aligned offset (chunk boundary) by resuming
    # the counter at offset//16, must match the one-shot ciphertext exactly.
    split = 256  # multiple of 16
    part1 = crypto.aes_ctr_crypt(plaintext[:split], key, nonce, counter_start=0)
    part2 = crypto.aes_ctr_crypt(plaintext[split:], key, nonce, counter_start=split // 16)
    assert part1 + part2 == ciphertext_full


def test_init_mega_link_key_xor_halves():
    # Manually construct a 32-byte key and verify the XOR-halves derivation
    # matches a hand-computed expectation (mirrors CryptTools.initMEGALinkKey).
    words = list(range(1, 9))
    key_bytes = crypto.i32a2bin(words)
    derived = crypto.init_mega_link_key(key_bytes)
    expected = crypto.i32a2bin([words[0] ^ words[4], words[1] ^ words[5], words[2] ^ words[6], words[3] ^ words[7]])
    assert derived == expected

    iv = crypto.init_mega_link_key_iv(key_bytes)
    assert iv == crypto.i32a2bin([words[4], words[5]])


def test_mega_user_hash_deterministic_and_word_extraction():
    password_aes = [0x11111111, 0x22222222, 0x33333333, 0x44444444]
    h1 = crypto.mega_user_hash("test@example.com", password_aes)
    h2 = crypto.mega_user_hash("TEST@EXAMPLE.COM", password_aes)
    assert h1 == h2  # lowercased before hashing
    assert isinstance(h1, str)
    decoded = crypto.url_base64_to_bin(h1)
    assert len(decoded) == 8  # two words


def test_mega_prepare_master_key_v1_shape():
    key = crypto.mega_prepare_master_key_v1(b"hunter2")
    assert len(key) == 4
    assert all(isinstance(w, int) for w in key)


def test_attr_encrypt_decrypt_roundtrip():
    key = bytes(range(16))
    payload = json.dumps({"n": "some file name.txt"}).encode("utf-8")
    enc = crypto.encrypt_attr(payload, key)
    dec = crypto.decrypt_attr(enc, key)
    assert json.loads(dec) == {"n": "some file name.txt"}


def test_at_rest_encrypt_decrypt_roundtrip():
    key = crypto.derive_master_key("my master password", b"somesalt12345678")
    blob = crypto.encrypt_at_rest(b"super secret account data", key)
    assert crypto.decrypt_at_rest(blob, key) == b"super secret account data"


def test_mpi_roundtrip_multi_value_buffer():
    # Build a buffer holding two concatenated MPIs the way MEGA's privk blob does.
    import struct

    def encode_mpi(value: int) -> bytes:
        raw = value.to_bytes((value.bit_length() + 7) // 8 or 1, "big")
        return struct.pack(">H", value.bit_length()) + raw

    a, b = 123456789, 42
    buf = encode_mpi(a) + encode_mpi(b)
    val_a, rest = crypto.mpi_to_int(buf)
    val_b, rest2 = crypto.mpi_to_int(rest)
    assert val_a == a
    assert val_b == b
    assert rest2 == b""


def test_rsa_decrypt_csid_roundtrip():
    import os
    import struct

    from Crypto.PublicKey import RSA

    key = RSA.generate(1024)
    p, q, d = int(key.p), int(key.q), int(key.d)
    n = p * q
    modulus_byte_len = (n.bit_length() + 7) // 8

    # MEGA's RSA plaintext block is `modulus_byte_len` bytes wide with the
    # 43-byte session id at the *front* and filler after it -- not a small
    # integer with the sid packed at the end. Force a leading zero byte so
    # this also exercises the Java "strip one leading zero" branch.
    session_id = bytes(range(43))
    filler = os.urandom(modulus_byte_len - 1 - 43)
    plaintext_block = b"\x00" + session_id + filler
    m = int.from_bytes(plaintext_block, "big")
    assert m < n

    # Encrypt with the public exponent to build a synthetic "csid" ciphertext.
    ciphertext_int = pow(m, int(key.e), n)
    ciphertext_bytes = ciphertext_int.to_bytes((ciphertext_int.bit_length() + 7) // 8, "big")
    csid_mpi = struct.pack(">H", ciphertext_int.bit_length()) + ciphertext_bytes

    privk = crypto.RSAPrivateComponents(p=p, q=q, d=d, u=0)
    raw_sid = crypto.rsa_decrypt_csid(csid_mpi, privk)
    assert raw_sid[:43] == session_id


def test_hashcash_solver_self_consistent():
    import base64
    import hashlib
    import os
    import struct

    token = os.urandom(48)
    token_b64 = crypto.bin_to_url_base64(token)
    # High easiness => large threshold => success within a handful of tries.
    # Real MEGA challenges use ~192; low easiness values (as MEGA also sends)
    # need millions of ~12.5MB SHA-256 attempts and are unsuitable for a test.
    easiness = 255
    header = f"1:{easiness}:1234567890:{token_b64}"

    solution = hashcash.solve(header, max_nonce=2**24)
    version, resp_token, nonce_b64 = solution.split(":")
    assert version == "1"
    assert resp_token == token_b64

    nonce_bytes = crypto.url_base64_to_bin(nonce_b64)
    buffer_tail = token * hashcash.TOKEN_REPEAT
    digest = hashlib.sha256(nonce_bytes + buffer_tail).digest()
    value = struct.unpack(">I", digest[:4])[0]
    threshold = hashcash._threshold(easiness)
    assert value <= threshold


def test_link_parser_legacy_file():
    link = "https://mega.nz/#!ABC123!SoMeKeyBase64Url"
    parsed = parse_mega_link(link)
    assert parsed.kind == "file"
    assert parsed.handle == "ABC123"
    assert parsed.key == "SoMeKeyBase64Url"
    assert parsed.folder_id is None


def test_link_parser_modern_file_converted_to_legacy():
    link = "https://mega.nz/file/ABC123#SoMeKey"
    legacy = new_links_to_legacy(link)
    assert legacy == "https://mega.nz/#!ABC123!SoMeKey"
    parsed = parse_mega_link(link)
    assert parsed.kind == "file"
    assert parsed.handle == "ABC123"
    assert parsed.key == "SoMeKey"


def test_link_parser_modern_folder_file():
    link = "https://mega.nz/folder/FOLDID#FOLDKEY/file/FILEID"
    parsed = parse_mega_link(link)
    assert parsed.kind == "file"
    assert parsed.handle == "FILEID"
    assert parsed.key == "FOLDKEY"
    assert parsed.folder_id == "FOLDID"


def test_link_parser_folder():
    link = "https://mega.nz/folder/FOLDID#FOLDKEY"
    parsed = parse_mega_link(link)
    assert parsed.kind == "folder"
    assert parsed.handle == "FOLDID"
    assert parsed.key == "FOLDKEY"


def test_link_parser_scoped_internal_file():
    from app.core.link_parser import build_scoped_file_link

    link = build_scoped_file_link("FH", "FK", "FOLD")
    parsed = parse_mega_link(link)
    assert parsed.kind == "file"
    assert parsed.handle == "FH"
    assert parsed.key == "FK"
    assert parsed.folder_id == "FOLD"
