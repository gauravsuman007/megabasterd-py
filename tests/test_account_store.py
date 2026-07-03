import pytest

from app.storage.account_store import AccountStore, LockedError
from app.storage.db import Database


async def _make_db(tmp_path):
    db = Database(tmp_path / "test.db")
    await db.connect()
    return db


@pytest.mark.asyncio
async def test_settings_roundtrip(tmp_path):
    db = await _make_db(tmp_path)
    assert await db.get_setting("foo") is None
    await db.set_setting("foo", "bar")
    assert await db.get_setting("foo") == "bar"
    await db.set_setting("foo", "baz")
    assert await db.get_setting("foo") == "baz"
    await db.close()


@pytest.mark.asyncio
async def test_persist_and_list_accounts_without_master_password(tmp_path):
    db = await _make_db(tmp_path)
    store = AccountStore(db)

    assert not await store.is_encrypted()
    assert not await store.is_locked()

    await store.persist_mega_login("user@example.com", "hunter2", [1, 2, 3, 4], "dXNlcl9oYXNo")

    accounts = await store.list_mega_accounts()
    assert accounts["user@example.com"]["password"] == "hunter2"
    assert accounts["user@example.com"]["password_aes"] == [1, 2, 3, 4]
    assert accounts["user@example.com"]["user_hash"] == "dXNlcl9oYXNo"

    await db.close()


@pytest.mark.asyncio
async def test_master_password_encrypts_and_roundtrips(tmp_path):
    db = await _make_db(tmp_path)
    store = AccountStore(db)

    await store.persist_mega_login("user@example.com", "hunter2", [10, 20, 30, 40], "dXNlcl9oYXNo")
    await store.set_master_password("correct horse battery staple")

    assert await store.is_encrypted()
    assert not await store.is_locked()  # still unlocked right after setting it

    accounts = await store.list_mega_accounts()
    assert accounts["user@example.com"]["password"] == "hunter2"
    assert accounts["user@example.com"]["password_aes"] == [10, 20, 30, 40]
    assert accounts["user@example.com"]["user_hash"] == "dXNlcl9oYXNo"

    # Raw DB rows must actually be ciphertext now, not plaintext.
    raw = (await db.get_mega_accounts())[0]
    assert raw["password"] != "hunter2"

    await db.close()


@pytest.mark.asyncio
async def test_lock_unlock_cycle(tmp_path):
    db = await _make_db(tmp_path)
    store = AccountStore(db)
    await store.persist_mega_login("user@example.com", "hunter2", [1, 2, 3, 4], "dXNlcl9oYXNo")
    await store.set_master_password("s3cr3t")

    store.lock()
    assert await store.is_locked()
    with pytest.raises(LockedError):
        await store.list_mega_accounts()
    with pytest.raises(LockedError):
        await store.persist_mega_login("other@example.com", "x", [1, 1, 1, 1], "aGFzaA")

    assert await store.unlock("wrong password") is False
    assert await store.is_locked()

    assert await store.unlock("s3cr3t") is True
    assert not await store.is_locked()
    accounts = await store.list_mega_accounts()
    assert accounts["user@example.com"]["password"] == "hunter2"

    await db.close()


@pytest.mark.asyncio
async def test_rotate_master_password_migrates_existing_accounts(tmp_path):
    db = await _make_db(tmp_path)
    store = AccountStore(db)
    await store.persist_mega_login("user@example.com", "hunter2", [5, 6, 7, 8], "dXNlcl9oYXNo")
    await store.set_master_password("first-password")

    await store.set_master_password("second-password")  # rotate while unlocked

    # Old password no longer unlocks a fresh store instance.
    store2 = AccountStore(db)
    assert await store2.unlock("first-password") is False
    assert await store2.unlock("second-password") is True
    accounts = await store2.list_mega_accounts()
    assert accounts["user@example.com"]["password"] == "hunter2"
    assert accounts["user@example.com"]["password_aes"] == [5, 6, 7, 8]

    await db.close()


@pytest.mark.asyncio
async def test_remove_master_password_restores_plaintext_storage(tmp_path):
    db = await _make_db(tmp_path)
    store = AccountStore(db)
    await store.persist_mega_login("user@example.com", "hunter2", [1, 2, 3, 4], "dXNlcl9oYXNo")
    await store.set_master_password("s3cr3t")

    await store.set_master_password(None)

    assert not await store.is_encrypted()
    assert not await store.is_locked()
    raw = (await db.get_mega_accounts())[0]
    assert raw["password"] == "hunter2"
    accounts = await store.list_mega_accounts()
    assert accounts["user@example.com"]["password_aes"] == [1, 2, 3, 4]

    await db.close()
