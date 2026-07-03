"""SmartProxy pool manager, ported from SmartMegaProxyManager.java.

Parses a "custom_proxy_list" text blob (one entry per line) into a live
pool of proxy addresses, supporting:
  - inline entries: ``[*]host:port[@b64user:b64pass]`` (legacy syntax,
    ``*`` marks SOCKS) or scheme-prefixed (``http://``, ``https://``,
    ``socks[45a]?://``)
  - remote list sources: a line ``#https://.../list.txt`` is fetched and
    every line in the response parsed the same way

Selection (`pick_proxy`) skips currently-banned entries; `block_proxy`
either removes a proxy (ban_time == 0) or marks it banned for `ban_time`
seconds. All the entry-parsing edge cases (stray '@', URL-style
user:pass@host:port rejection, path suffix stripping) are preserved
exactly since a subtly wrong parse silently drops or misroutes proxies.
"""
from __future__ import annotations

import base64
import random
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import quote

_HOST_PORT_RE = re.compile(r".+?:[0-9]{1,5}$")

_SCHEME_PREFIXES: list[tuple[str, bool]] = [
    ("http://", False),
    ("https://", False),
    ("socks5://", True),
    ("socks4a://", True),
    ("socks4://", True),
    ("socks://", True),
]

DEFAULT_BAN_TIME = 300
DEFAULT_AUTOREFRESH_MINUTES = 60
DEFAULT_RECHECK_509_WINDOW = 3600


@dataclass
class ProxyEntry:
    """A proxy's live state in the pool: when it was last banned (-1 = never)
    and whether it's a SOCKS proxy."""
    ban_timestamp: float = -1.0  # -1 == never banned
    is_socks: bool = False


@dataclass
class ParsedProxy:
    """Result of parsing one proxy line: the ``host:port`` `address`, whether
    it's SOCKS, and the optional ``b64user:b64pass`` auth trailer."""
    address: str  # "host:port"
    is_socks: bool
    auth: str | None  # "b64user:b64pass", if present


def parse_proxy_entry(raw: str) -> ParsedProxy | None:
    """Parse one line. Returns None for blanks, '#...' source lines, and
    malformed entries (mirrors parseProxyEntry's silent-skip + warning-log
    behavior in Java; here callers can log if they want)."""
    if raw is None:
        return None
    line = raw.strip()
    if not line or line.startswith("#"):
        return None

    socks = False
    had_scheme = False

    if line.startswith("*"):
        socks = True
        line = line[1:].strip()

    lower = line.lower()
    for prefix, is_socks in _SCHEME_PREFIXES:
        if lower.startswith(prefix):
            line = line[len(prefix) :]
            had_scheme = True
            if is_socks:
                socks = True
            break

    if "@" in line:
        parts = line.split("@")
        if len(parts) != 2:
            return None  # stray '@', can't disambiguate

        hostport = parts[0]
        slash = hostport.find("/")
        if slash >= 0:
            hostport = hostport[:slash]
        hostport = hostport.strip()

        if not _HOST_PORT_RE.match(hostport):
            return None

        # URL-style user:pass@host:port masquerading as the legacy trailer form.
        if had_scheme and _HOST_PORT_RE.match(parts[1]):
            return None

        return ParsedProxy(address=hostport, is_socks=socks, auth=parts[1])

    if had_scheme:
        slash = line.find("/")
        if slash >= 0:
            line = line[:slash]
    line = line.strip()

    if _HOST_PORT_RE.match(line):
        return ParsedProxy(address=line, is_socks=socks, auth=None)
    return None


@dataclass
class RefreshResult:
    """Outcome of a pool refresh: how many `entries` are now live, how many
    remote source URLs succeeded/failed, and whether the previous pool was
    kept because the new parse yielded nothing usable."""
    entries: int
    urls_ok: int
    urls_failed: int
    preserved_previous: bool


FetchFn = Callable[[str], Awaitable[str]]
VerifiedPoolProvider = Callable[[], list[str]]


def parse_proxy_list_addresses(text: str) -> list[tuple[str, bool]]:
    """Parse a raw settings-textarea blob into (address, is_socks) pairs,
    skipping blanks, malformed lines, and '#URL' source lines (those need
    an async fetch to resolve, so callers that only want the *inline*
    entries -- e.g. to prioritize them for diagnostics testing -- use
    this instead of refresh_from_text)."""
    addresses = []
    for line in (text or "").splitlines():
        parsed = parse_proxy_entry(line)
        if parsed is not None:
            addresses.append((parsed.address, parsed.is_socks))
    return addresses


class SmartProxyManager:
    """Live proxy pool with banning and selection.

    Holds the configured `_pool` (parsed from the user's list), tracks bans
    per entry, and hands out one usable proxy at a time via `pick_proxy`.
    When `verified_pool_provider` is set (diagnostics enabled), reachable
    proxies from that provider are preferred over the configured pool.
    """

    def __init__(
        self,
        ban_time: int = DEFAULT_BAN_TIME,
        proxy_timeout: int = 45,
        force_smart_proxy: bool = False,
        autorefresh_minutes: int = DEFAULT_AUTOREFRESH_MINUTES,
        random_select: bool = True,
        reset_slot_proxy: bool = True,
        recheck_509_window: int = DEFAULT_RECHECK_509_WINDOW,
    ):
        self.ban_time = ban_time
        self.proxy_timeout = proxy_timeout
        self.force_smart_proxy = force_smart_proxy
        self.autorefresh_minutes = autorefresh_minutes
        self.random_select = random_select
        self.reset_slot_proxy = reset_slot_proxy
        self.recheck_509_window = recheck_509_window

        self._pool: dict[str, ProxyEntry] = {}
        self._auth: dict[str, str] = {}
        self.last_refresh_timestamp: float = 0.0

        # When set (see app.state.sync_verified_pool_provider), pick_proxy
        # tries these addresses -- diagnostics-confirmed reachable to
        # mega.nz -- before falling back to the normal configured _pool.
        # A callable rather than a plain list so it always reflects the
        # diagnostics tester's *current* pool without this module having
        # to import or poll it directly.
        self.verified_pool_provider: VerifiedPoolProvider | None = None
        # Ban tracking for verified-pool picks, which live outside _pool
        # (they're not part of the user's configured list) -- otherwise a
        # verified proxy that 509s would have nowhere to record that and
        # would just get picked again immediately.
        self._verified_bans: dict[str, float] = {}

    # -- pool introspection -------------------------------------------------

    def proxy_count(self) -> int:
        """Total configured proxies (banned or not)."""
        return len(self._pool)

    def count_blocked(self) -> int:
        """How many configured proxies are currently banned."""
        now = time.time()
        return sum(1 for e in self._pool.values() if self._is_banned(e, now))

    def snapshot(self) -> list[tuple[str, str]]:
        """(address, "socks"|"http") for every configured proxy, for display."""
        return [(addr, "socks" if e.is_socks else "http") for addr, e in self._pool.items()]

    def _is_banned(self, entry: ProxyEntry, now: float) -> bool:
        """True while `entry`'s ban is still within the ban_time window."""
        return entry.ban_timestamp != -1 and entry.ban_timestamp >= now - self.ban_time

    def _is_verified_banned(self, address: str, now: float) -> bool:
        """True while a verified-pool proxy's ban is still active (inf = dropped
        permanently when ban_time is 0)."""
        ts = self._verified_bans.get(address)
        if ts is None:
            return False
        return ts == float("inf") or ts >= now - self.ban_time

    # -- selection ------------------------------------------------------

    def pick_proxy(self, excluded: set[str] | None = None) -> tuple[str, str] | None:
        """Pure, non-blocking pick: one usable (non-banned, non-excluded)
        proxy, or None if none is available right now.

        If a verified_pool_provider is set (diagnostics enabled), its
        addresses are tried first; only if none of those are usable right
        now does this fall back to the normally configured pool."""
        now = time.time()
        excluded = excluded or set()

        if self.verified_pool_provider is not None:
            candidates = [c for line in self.verified_pool_provider() if (c := parse_proxy_entry(line)) is not None]
            if self.random_select:
                random.shuffle(candidates)
            for candidate in candidates:
                if candidate.address in excluded or self._is_verified_banned(candidate.address, now):
                    continue
                return candidate.address, ("socks" if candidate.is_socks else "http")

        if not self._pool:
            return None

        keys = list(self._pool.keys())
        if self.random_select:
            random.shuffle(keys)

        for key in keys:
            entry = self._pool.get(key)
            if entry is None or key in excluded:
                continue
            if not self._is_banned(entry, now):
                return key, ("socks" if entry.is_socks else "http")
        return None

    def block_proxy(self, address: str, cause: str = "") -> None:
        """Ban a proxy after it failed (e.g. a 509 or connection error). With
        ban_time == 0 the proxy is dropped outright; otherwise it's marked
        banned for ban_time seconds. Verified-pool proxies (not in `_pool`)
        are recorded in a separate ban map so they aren't re-picked at once."""
        entry = self._pool.get(address)
        if entry is not None:
            if self.ban_time == 0:
                del self._pool[address]
            else:
                entry.ban_timestamp = time.time()
            return
        # Not in the configured pool -- must have come from
        # verified_pool_provider, tracked separately (see __init__).
        self._verified_bans[address] = float("inf") if self.ban_time == 0 else time.time()

    # -- refresh --------------------------------------------------------

    async def refresh_from_text(self, custom_proxy_list: str | None, fetch: FetchFn) -> RefreshResult:
        """Parse `custom_proxy_list` (the raw settings textarea) and swap
        in the resulting pool. `fetch(url)` is called for every '#URL'
        source line and must return the response body as text; a failing
        fetch is logged/counted but doesn't abort the whole refresh.
        If the result set is empty, the previous pool is preserved."""
        new_pool: dict[str, ProxyEntry] = {}
        new_auth: dict[str, str] = {}
        urls: list[str] = []
        had_input = False

        for line in (custom_proxy_list or "").splitlines():
            trimmed = line.strip()
            if not trimmed:
                continue
            had_input = True
            if trimmed.startswith("#"):
                url_part = trimmed[1:].strip()
                if url_part.lower().startswith(("http://", "https://")):
                    urls.append(url_part)
            else:
                self._merge_entry(trimmed, new_pool, new_auth)

        urls_ok = 0
        urls_failed = 0
        for url in urls:
            try:
                body = await fetch(url)
                for line in body.splitlines():
                    self._merge_entry(line, new_pool, new_auth)
                urls_ok += 1
            except Exception:
                urls_failed += 1

        preserved = False
        if new_pool:
            self._pool = new_pool
            self._auth = new_auth
        else:
            preserved = had_input  # nothing usable, but there was input -> keep old pool

        self.last_refresh_timestamp = time.time()
        return RefreshResult(entries=len(self._pool), urls_ok=urls_ok, urls_failed=urls_failed, preserved_previous=preserved)

    @staticmethod
    def _merge_entry(line: str, into_pool: dict[str, ProxyEntry], into_auth: dict[str, str]) -> None:
        """Parse one line and, if valid, add it to the pool/auth maps being
        built (a no-op for blanks, comments, and malformed lines)."""
        parsed = parse_proxy_entry(line)
        if parsed is None:
            return
        into_pool[parsed.address] = ProxyEntry(ban_timestamp=-1, is_socks=parsed.is_socks)
        if parsed.auth:
            into_auth[parsed.address] = parsed.auth

    def get_auth(self, address: str) -> str | None:
        """The ``b64user:b64pass`` auth trailer for `address`, if it had one."""
        return self._auth.get(address)

    def build_proxy_url(self, address: str, is_socks: bool) -> str:
        """httpx-compatible proxy URL (``http://`` or ``socks5://``), with
        the legacy base64 user/pass trailer decoded into userinfo if set."""
        scheme = "socks5" if is_socks else "http"
        auth = self._auth.get(address)
        if auth:
            user_b64, pass_b64 = auth.split(":", 1)
            user = base64.b64decode(user_b64).decode("utf-8")
            password = base64.b64decode(pass_b64).decode("utf-8")
            return f"{scheme}://{quote(user)}:{quote(password)}@{address}"
        return f"{scheme}://{address}"
