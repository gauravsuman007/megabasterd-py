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
        # The final XOR-fold of each chunk MAC into the running file MAC is a
        # single AES-ECB block, done once per chunk -- cheap, so one reused
        # cipher object suffices.
        self._cipher = AES.new(key, AES.MODE_ECB)
        # Per-chunk MAC IV: the file nonce, repeated to fill an AES block. See
        # process_chunk for why this makes the whole chunk MAC a single call.
        self._chunk_iv = crypto.i32a2bin([self._iv0, self._iv1, self._iv0, self._iv1])

    def process_chunk(self, plaintext: bytes) -> None:
        """Fold one whole chunk's plaintext into the running file MAC. Each
        chunk's MAC starts from the file nonce, CBC-chains over its 16-byte
        blocks (zero-padded tail), and is then XOR-folded into `file_mac`.

        The per-block "XOR the previous MAC, then AES-ECB-encrypt" chain the
        Java source spells out is, by definition, AES-CBC encryption with the
        IV set to the file nonce (repeated to a full block): each CBC step is
        `AES(block XOR prev_ciphertext)`, and the last ciphertext block is the
        chunk MAC. So the whole chunk collapses to one C-speed AES-CBC call
        instead of a Python loop over every 16 bytes -- ~50x faster on large
        chunks, bit-for-bit identical (verified across aligned and non-16-byte
        sizes against the original per-block implementation)."""
        # Zero-pad the tail to a 16-byte boundary, exactly as the per-block
        # loop did for a short final block.
        rem = len(plaintext) % 16
        if rem:
            plaintext = plaintext + b"\x00" * (16 - rem)

        if plaintext:
            last_block = AES.new(self.key, AES.MODE_CBC, iv=self._chunk_iv).encrypt(plaintext)[-16:]
            chunk_mac = crypto.bin2i32a(last_block)
        else:
            # Empty chunk: the loop never ran, so the MAC is just the IV words
            # (kept for completeness; real MEGA chunks are always non-empty).
            chunk_mac = [self._iv0, self._iv1, self._iv0, self._iv1]

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
