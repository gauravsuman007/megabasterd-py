"""Incremental CBC-MAC over a file's plaintext, ported from the chunk-MAC
chaining in UploadMACGenerator.java / Download.java's verifyFileCBCMAC.

Used both directions: uploads compute this over the source file to submit
as the upload's meta_mac; downloads compute it over the decrypted output to
verify integrity against the meta_mac embedded in the file's link key.

Must be fed one whole MEGA chunk at a time (per app.core.chunks), in
ascending chunk order -- the per-chunk MAC state resets to the file's
nonce at the start of each chunk, and the running file-MAC only advances
once a full chunk has been folded in.
"""
from __future__ import annotations

from Crypto.Cipher import AES

from app.core import crypto


class FileMacGenerator:
    """Accumulates the file's CBC-MAC as chunks are fed in, in ascending chunk
    order (see module docstring). Feed each MEGA chunk to `process_chunk`, then
    read `meta_mac`/`meta_mac_bytes` once the whole file has passed through."""

    def __init__(self, key: bytes, nonce_words: tuple[int, int]):
        self.key = key
        self._iv0, self._iv1 = nonce_words
        self.file_mac = [0, 0, 0, 0]
        # One block (16 bytes) is AES-ECB-"encrypted" per 16 bytes of plaintext,
        # sequentially (each block's ciphertext feeds the next) -- this cannot
        # be vectorized/batched, so avoiding AES.new()'s key-schedule setup on
        # every call (reusing one cipher object instead) is what keeps this
        # from being the dominant cost on multi-GB files.
        self._cipher = AES.new(key, AES.MODE_ECB)

    def process_chunk(self, plaintext: bytes) -> None:
        """Fold one whole chunk's plaintext into the running file MAC. Each
        chunk's MAC starts from the file nonce, CBC-chains over its 16-byte
        blocks (zero-padded tail), and is then XOR-folded into `file_mac`."""
        chunk_mac = [self._iv0, self._iv1, self._iv0, self._iv1]

        for i in range(0, len(plaintext), 16):
            block = plaintext[i : i + 16]
            if len(block) < 16:
                block = block + b"\x00" * (16 - len(block))
            block_words = crypto.bin2i32a(block)
            chunk_mac = [chunk_mac[j] ^ block_words[j] for j in range(4)]
            chunk_mac = crypto.bin2i32a(self._cipher.encrypt(crypto.i32a2bin(chunk_mac)))

        self.file_mac = [self.file_mac[j] ^ chunk_mac[j] for j in range(4)]
        self.file_mac = crypto.bin2i32a(self._cipher.encrypt(crypto.i32a2bin(self.file_mac)))

    @property
    def meta_mac(self) -> tuple[int, int]:
        """The 2-word meta MAC (file MAC folded to 8 bytes) MEGA stores/verifies."""
        return (self.file_mac[0] ^ self.file_mac[1], self.file_mac[2] ^ self.file_mac[3])

    @property
    def meta_mac_bytes(self) -> bytes:
        """`meta_mac` as 8 raw bytes."""
        return crypto.i32a2bin(list(self.meta_mac))
