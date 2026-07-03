"""SQLite persistence, ported from DBTools.java / SqliteSingleton.java.

Schema is intentionally identical to the Java version's tables/columns so
existing knowledge of the original project maps directly onto this one.
Uses a single shared aiosqlite connection (WAL mode) -- SQLite serializes
writers anyway, and this is a single-process local app, so one connection
protected by an asyncio.Lock is simpler than a pool for no real cost.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import aiosqlite

DEFAULT_DB_PATH = Path(os.environ.get("MEGABASTERD_DB_PATH", str(Path.home() / ".megabasterd-py" / "megabasterd.db")))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS downloads(
    url TEXT, email TEXT, path TEXT, filename TEXT, filekey TEXT,
    filesize UNSIGNED BIG INT, filepass VARCHAR(64), filenoexpire VARCHAR(64),
    custom_chunks_dir TEXT,
    PRIMARY KEY (url), UNIQUE(path, filename)
);
CREATE TABLE IF NOT EXISTS uploads(
    filename TEXT, email TEXT, url TEXT, ul_key TEXT, parent_node TEXT,
    root_node TEXT, share_key TEXT, folder_link TEXT,
    bytes_uploaded UNSIGNED BIG INT, meta_mac TEXT,
    PRIMARY KEY (filename), UNIQUE(filename, email)
);
CREATE TABLE IF NOT EXISTS settings(
    key VARCHAR(255), value TEXT, PRIMARY KEY(key)
);
CREATE TABLE IF NOT EXISTS mega_accounts(
    email TEXT, password TEXT, password_aes TEXT, user_hash TEXT,
    PRIMARY KEY(email)
);
CREATE TABLE IF NOT EXISTS elc_accounts(
    host TEXT, user TEXT, apikey TEXT, PRIMARY KEY(host)
);
CREATE TABLE IF NOT EXISTS mega_sessions(
    email TEXT, ma BLOB, crypt INT, PRIMARY KEY(email)
);
CREATE TABLE IF NOT EXISTS downloads_queue(url TEXT, PRIMARY KEY(url));
CREATE TABLE IF NOT EXISTS uploads_queue(filename TEXT, PRIMARY KEY(filename));
-- Live download queue, keyed by the in-memory transfer id (a UUID) rather
-- than by url, so duplicate links and per-transfer status/dest survive a
-- restart. bytes_done is deliberately NOT stored -- it's recovered from the
-- partial file's size on resume, so progress callbacks never touch the DB.
CREATE TABLE IF NOT EXISTS download_queue(
    id TEXT PRIMARY KEY,
    link TEXT NOT NULL,
    name TEXT,
    path TEXT,
    total UNSIGNED BIG INT DEFAULT 0,
    status TEXT
);
"""


class Database:
    def __init__(self, path: Path | str = DEFAULT_DB_PATH):
        self.path = Path(path)
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected -- call connect() first")
        return self._conn

    # -- settings ---------------------------------------------------------

    async def get_setting(self, key: str) -> str | None:
        async with self._lock:
            cur = await self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = await cur.fetchone()
            return row["value"] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        async with self._lock:
            await self.conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            await self.conn.commit()

    async def delete_setting(self, key: str) -> None:
        async with self._lock:
            await self.conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            await self.conn.commit()

    async def get_all_settings(self) -> dict[str, str]:
        async with self._lock:
            cur = await self.conn.execute("SELECT key, value FROM settings")
            rows = await cur.fetchall()
            return {row["key"]: row["value"] for row in rows}

    # -- mega_accounts ------------------------------------------------------

    async def upsert_mega_account(self, email: str, password: str | None, password_aes: str | None, user_hash: str | None) -> None:
        async with self._lock:
            await self.conn.execute(
                """INSERT INTO mega_accounts(email, password, password_aes, user_hash) VALUES (?, ?, ?, ?)
                   ON CONFLICT(email) DO UPDATE SET password=excluded.password, password_aes=excluded.password_aes, user_hash=excluded.user_hash""",
                (email, password, password_aes, user_hash),
            )
            await self.conn.commit()

    async def get_mega_accounts(self) -> list[dict]:
        async with self._lock:
            cur = await self.conn.execute("SELECT email, password, password_aes, user_hash FROM mega_accounts")
            rows = await cur.fetchall()
            return [dict(row) for row in rows]

    async def delete_mega_account(self, email: str) -> None:
        async with self._lock:
            await self.conn.execute("DELETE FROM mega_accounts WHERE email = ?", (email,))
            await self.conn.execute("DELETE FROM mega_sessions WHERE email = ?", (email,))
            await self.conn.commit()

    # -- mega_sessions --------------------------------------------------

    async def upsert_mega_session(self, email: str, session_blob: bytes, encrypted: bool) -> None:
        async with self._lock:
            await self.conn.execute(
                """INSERT INTO mega_sessions(email, ma, crypt) VALUES (?, ?, ?)
                   ON CONFLICT(email) DO UPDATE SET ma=excluded.ma, crypt=excluded.crypt""",
                (email, session_blob, int(encrypted)),
            )
            await self.conn.commit()

    async def get_mega_session(self, email: str) -> dict | None:
        async with self._lock:
            cur = await self.conn.execute("SELECT email, ma, crypt FROM mega_sessions WHERE email = ?", (email,))
            row = await cur.fetchone()
            return dict(row) if row else None

    async def truncate_mega_sessions(self) -> None:
        async with self._lock:
            await self.conn.execute("DELETE FROM mega_sessions")
            await self.conn.commit()

    # -- elc_accounts -----------------------------------------------------

    async def upsert_elc_account(self, host: str, user: str, apikey: str) -> None:
        async with self._lock:
            await self.conn.execute(
                """INSERT INTO elc_accounts(host, user, apikey) VALUES (?, ?, ?)
                   ON CONFLICT(host) DO UPDATE SET user=excluded.user, apikey=excluded.apikey""",
                (host, user, apikey),
            )
            await self.conn.commit()

    async def get_elc_accounts(self) -> list[dict]:
        async with self._lock:
            cur = await self.conn.execute("SELECT host, user, apikey FROM elc_accounts")
            rows = await cur.fetchall()
            return [dict(row) for row in rows]

    async def delete_elc_account(self, host: str) -> None:
        async with self._lock:
            await self.conn.execute("DELETE FROM elc_accounts WHERE host = ?", (host,))
            await self.conn.commit()

    # -- downloads / uploads (resume bookkeeping) --------------------------

    async def insert_download(
        self, url: str, email: str | None, path: str, filename: str, filekey: str,
        size: int, filepass: str | None = None, filenoexpire: str | None = None, custom_chunks_dir: str | None = None,
    ) -> None:
        async with self._lock:
            await self.conn.execute(
                """INSERT OR REPLACE INTO downloads(url, email, path, filename, filekey, filesize, filepass, filenoexpire, custom_chunks_dir)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (url, email, path, filename, filekey, size, filepass, filenoexpire, custom_chunks_dir),
            )
            await self.conn.commit()

    async def delete_download(self, url: str) -> None:
        async with self._lock:
            await self.conn.execute("DELETE FROM downloads WHERE url = ?", (url,))
            await self.conn.commit()

    async def get_downloads(self) -> list[dict]:
        async with self._lock:
            cur = await self.conn.execute("SELECT * FROM downloads")
            rows = await cur.fetchall()
            return [dict(row) for row in rows]

    async def insert_upload(
        self, filename: str, email: str | None, parent_node: str, ul_key: str,
        root_node: str, share_key: str | None, folder_link: str | None,
    ) -> None:
        async with self._lock:
            await self.conn.execute(
                """INSERT OR REPLACE INTO uploads(filename, email, parent_node, ul_key, root_node, share_key, folder_link, bytes_uploaded, meta_mac)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL)""",
                (filename, email, parent_node, ul_key, root_node, share_key, folder_link),
            )
            await self.conn.commit()

    async def update_upload_progress(self, filename: str, email: str | None, bytes_uploaded: int, meta_mac: str | None) -> None:
        async with self._lock:
            await self.conn.execute(
                "UPDATE uploads SET bytes_uploaded = ?, meta_mac = ? WHERE filename = ? AND (email = ? OR (email IS NULL AND ? IS NULL))",
                (bytes_uploaded, meta_mac, filename, email, email),
            )
            await self.conn.commit()

    async def delete_upload(self, filename: str, email: str | None) -> None:
        async with self._lock:
            await self.conn.execute(
                "DELETE FROM uploads WHERE filename = ? AND (email = ? OR (email IS NULL AND ? IS NULL))",
                (filename, email, email),
            )
            await self.conn.commit()

    async def get_uploads(self) -> list[dict]:
        async with self._lock:
            cur = await self.conn.execute("SELECT * FROM uploads")
            rows = await cur.fetchall()
            return [dict(row) for row in rows]

    # -- download_queue (live queue, restored on startup) ------------------

    async def upsert_download_queue(self, id: str, link: str, name: str | None, path: str | None, total: int, status: str) -> None:
        async with self._lock:
            await self.conn.execute(
                """INSERT INTO download_queue(id, link, name, path, total, status) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET link=excluded.link, name=excluded.name,
                       path=excluded.path, total=excluded.total, status=excluded.status""",
                (id, link, name, path, total, status),
            )
            await self.conn.commit()

    async def delete_download_queue(self, id: str) -> None:
        async with self._lock:
            await self.conn.execute("DELETE FROM download_queue WHERE id = ?", (id,))
            await self.conn.commit()

    async def get_download_queue(self) -> list[dict]:
        async with self._lock:
            cur = await self.conn.execute("SELECT id, link, name, path, total, status FROM download_queue")
            rows = await cur.fetchall()
            return [dict(row) for row in rows]
