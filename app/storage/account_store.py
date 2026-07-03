"""MEGA account credential storage with optional master-password-at-rest
encryption, ported from AccountStore.java.

Storage quirk preserved from the Java version: when no master password is
configured, `password`/`password_aes`/`user_hash` are stored as plain
strings (password_aes as regular-base64 of the packed bytes, user_hash as
MEGA's own URL-base64). When a master password is configured, all three
are stored as regular-base64 of their AES-CBC-at-rest ciphertext instead.
Callers only ever see/pass plaintext -- this class handles the rest.
"""
from __future__ import annotations

import base64
import hashlib
import os

from app.core import crypto
from app.storage.db import Database

MASTER_PBKDF2_ITERATIONS = 65_536
MASTER_PBKDF2_SALT_BYTES = 16


class LockedError(Exception):
    """Raised when a plaintext read/write is attempted while the store is
    encrypted and no master password has been unlocked."""


class AccountStore:
    def __init__(self, db: Database):
        self.db = db
        self._master_key: bytes | None = None

    async def is_encrypted(self) -> bool:
        return await self.db.get_setting("master_pass_hash") is not None

    async def is_locked(self) -> bool:
        return await self.is_encrypted() and self._master_key is None

    def lock(self) -> None:
        self._master_key = None

    async def unlock(self, password: str) -> bool:
        salt_b64 = await self.db.get_setting("master_pass_salt")
        stored_hash = await self.db.get_setting("master_pass_hash")
        if salt_b64 is None or stored_hash is None:
            return False

        key = crypto.derive_master_key(password, base64.b64decode(salt_b64))
        candidate_hash = base64.b64encode(hashlib.sha1(key).digest()).decode("ascii")

        if not _constant_time_eq(candidate_hash, stored_hash):
            return False

        self._master_key = key
        return True

    async def set_master_password(self, new_password: str | None) -> None:
        """Set (new_password given), rotate, or remove (new_password=None)
        the master password, re-encrypting every stored account in place."""
        old_key = self._master_key
        was_encrypted = await self.is_encrypted()

        new_key: bytes | None = None
        if new_password is not None:
            salt = os.urandom(MASTER_PBKDF2_SALT_BYTES)
            new_key = crypto.derive_master_key(new_password, salt)
            new_hash = base64.b64encode(hashlib.sha1(new_key).digest()).decode("ascii")
            await self.db.set_setting("master_pass_salt", base64.b64encode(salt).decode("ascii"))
            await self.db.set_setting("master_pass_hash", new_hash)
        else:
            await self.db.delete_setting("master_pass_salt")
            await self.db.delete_setting("master_pass_hash")

        # Sessions are never re-encrypted -- just drop them; they get
        # refetched via fast_login on next use.
        await self.db.truncate_mega_sessions()

        for account in await self.db.get_mega_accounts():
            email = account["email"]
            if was_encrypted and old_key is not None:
                password = crypto.decrypt_at_rest(base64.b64decode(account["password"]), old_key).decode("utf-8")
                password_aes_bytes = crypto.decrypt_at_rest(base64.b64decode(account["password_aes"]), old_key)
                user_hash_bytes = crypto.decrypt_at_rest(base64.b64decode(account["user_hash"]), old_key)
                user_hash = crypto.bin_to_base64(user_hash_bytes)
            else:
                password = account["password"]
                password_aes_bytes = crypto.base64_to_bin(account["password_aes"])
                user_hash = account["user_hash"]
                # user_hash may be MEGA's URL-base64 form when never encrypted;
                # normalize to standard base64 bytes either way.
                user_hash_bytes = crypto.url_base64_to_bin(user_hash) if _looks_url_b64(user_hash) else crypto.base64_to_bin(user_hash)

            if new_key is not None:
                stored_password = crypto.bin_to_base64(crypto.encrypt_at_rest(password.encode("utf-8"), new_key))
                stored_password_aes = crypto.bin_to_base64(crypto.encrypt_at_rest(password_aes_bytes, new_key))
                stored_user_hash = crypto.bin_to_base64(crypto.encrypt_at_rest(user_hash_bytes, new_key))
            else:
                stored_password = password
                stored_password_aes = crypto.bin_to_base64(password_aes_bytes)
                stored_user_hash = crypto.bin_to_url_base64(user_hash_bytes)

            await self.db.upsert_mega_account(email, stored_password, stored_password_aes, stored_user_hash)

        self._master_key = new_key

    async def persist_mega_login(self, email: str, plaintext_password: str, password_aes_words: list[int], user_hash: str) -> None:
        if await self.is_locked():
            raise LockedError("account store is locked")

        password_aes_bytes = crypto.i32a2bin(password_aes_words)

        if self._master_key is not None:
            stored_password = crypto.bin_to_base64(crypto.encrypt_at_rest(plaintext_password.encode("utf-8"), self._master_key))
            stored_password_aes = crypto.bin_to_base64(crypto.encrypt_at_rest(password_aes_bytes, self._master_key))
            stored_user_hash = crypto.bin_to_base64(crypto.encrypt_at_rest(crypto.url_base64_to_bin(user_hash), self._master_key))
        else:
            stored_password = plaintext_password
            stored_password_aes = crypto.bin_to_base64(password_aes_bytes)
            stored_user_hash = user_hash

        await self.db.upsert_mega_account(email, stored_password, stored_password_aes, stored_user_hash)

    async def list_mega_accounts(self) -> dict[str, dict]:
        if await self.is_locked():
            raise LockedError("account store is locked")

        result: dict[str, dict] = {}
        for account in await self.db.get_mega_accounts():
            email = account["email"]
            if self._master_key is not None:
                password = crypto.decrypt_at_rest(base64.b64decode(account["password"]), self._master_key).decode("utf-8")
                password_aes_bytes = crypto.decrypt_at_rest(base64.b64decode(account["password_aes"]), self._master_key)
                user_hash_bytes = crypto.decrypt_at_rest(base64.b64decode(account["user_hash"]), self._master_key)
                user_hash = crypto.bin_to_url_base64(user_hash_bytes)
            else:
                password = account["password"]
                password_aes_bytes = crypto.base64_to_bin(account["password_aes"])
                user_hash = account["user_hash"]

            result[email] = {
                "password": password,
                "password_aes": crypto.bin2i32a(password_aes_bytes),
                "user_hash": user_hash,
            }
        return result

    async def delete_mega_account(self, email: str) -> None:
        await self.db.delete_mega_account(email)


def _constant_time_eq(a: str, b: str) -> bool:
    import hmac

    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _looks_url_b64(s: str) -> bool:
    return "-" in s or "_" in s
