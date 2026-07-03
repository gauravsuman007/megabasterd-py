import pytest

from app.core.proxy_manager import SmartProxyManager, parse_proxy_entry, parse_proxy_list_addresses


def test_parse_legacy_plain():
    p = parse_proxy_entry("1.2.3.4:8080")
    assert p is not None
    assert p.address == "1.2.3.4:8080"
    assert p.is_socks is False
    assert p.auth is None


def test_parse_legacy_socks_marker():
    p = parse_proxy_entry("*1.2.3.4:1080")
    assert p.is_socks is True
    assert p.address == "1.2.3.4:1080"


def test_parse_legacy_with_auth():
    p = parse_proxy_entry("1.2.3.4:8080@dXNlcg==:cGFzcw==")
    assert p.address == "1.2.3.4:8080"
    assert p.auth == "dXNlcg==:cGFzcw=="
    assert p.is_socks is False


def test_parse_scheme_http():
    p = parse_proxy_entry("http://1.2.3.4:8080")
    assert p.address == "1.2.3.4:8080"
    assert p.is_socks is False


def test_parse_scheme_https_maps_to_http_type():
    p = parse_proxy_entry("https://1.2.3.4:8080")
    assert p.address == "1.2.3.4:8080"
    assert p.is_socks is False


@pytest.mark.parametrize("scheme", ["socks://", "socks4://", "socks4a://", "socks5://"])
def test_parse_scheme_socks_variants(scheme):
    p = parse_proxy_entry(f"{scheme}1.2.3.4:1080")
    assert p.address == "1.2.3.4:1080"
    assert p.is_socks is True


def test_parse_scheme_with_trailing_path_stripped():
    p = parse_proxy_entry("http://1.2.3.4:8080/some/path")
    assert p.address == "1.2.3.4:8080"


def test_parse_scheme_with_auth_and_path_on_hostport_half():
    p = parse_proxy_entry("http://1.2.3.4:8080/foo@dXNlcg==:cGFzcw==")
    assert p.address == "1.2.3.4:8080"
    assert p.auth == "dXNlcg==:cGFzcw=="


def test_parse_rejects_url_style_userpass_auth():
    # scheme prefix + parts[1] itself looks like host:port -> reject
    p = parse_proxy_entry("http://user:1234@1.2.3.4:8080")
    assert p is None


def test_parse_rejects_stray_at():
    p = parse_proxy_entry("user@pass@1.2.3.4:8080")
    assert p is None


def test_parse_rejects_malformed_no_port():
    assert parse_proxy_entry("not-a-proxy") is None
    assert parse_proxy_entry("1.2.3.4") is None


def test_parse_ignores_blank_and_comment_lines():
    assert parse_proxy_entry("") is None
    assert parse_proxy_entry("   ") is None
    assert parse_proxy_entry("# a comment") is None
    assert parse_proxy_entry("#https://example.com/list.txt") is None


@pytest.mark.asyncio
async def test_refresh_parses_inline_entries():
    mgr = SmartProxyManager()

    async def fetch(url):
        raise AssertionError("should not be called, no #URL lines present")

    result = await mgr.refresh_from_text("1.2.3.4:8080\n*5.6.7.8:1080\n", fetch)
    assert result.entries == 2
    assert mgr.proxy_count() == 2
    snap = dict(mgr.snapshot())
    assert snap["1.2.3.4:8080"] == "http"
    assert snap["5.6.7.8:1080"] == "socks"


@pytest.mark.asyncio
async def test_refresh_fetches_url_sources_and_merges():
    mgr = SmartProxyManager()

    async def fetch(url):
        assert url == "https://example.com/list.txt"
        return "9.9.9.9:3128\n*8.8.8.8:1080\n"

    result = await mgr.refresh_from_text("1.2.3.4:8080\n#https://example.com/list.txt\n", fetch)
    assert result.urls_ok == 1
    assert result.urls_failed == 0
    assert result.entries == 3
    assert mgr.proxy_count() == 3


@pytest.mark.asyncio
async def test_refresh_url_failure_does_not_abort_others():
    mgr = SmartProxyManager()

    async def fetch(url):
        raise IOError("connection refused")

    result = await mgr.refresh_from_text("1.2.3.4:8080\n#https://dead.example.com/list.txt\n", fetch)
    assert result.urls_failed == 1
    assert result.entries == 1  # inline entry still made it in
    assert mgr.proxy_count() == 1


@pytest.mark.asyncio
async def test_refresh_preserves_previous_pool_on_empty_result():
    mgr = SmartProxyManager()

    async def fetch(url):
        return ""

    await mgr.refresh_from_text("1.2.3.4:8080\n", fetch)
    assert mgr.proxy_count() == 1

    # Second refresh with only garbage -> should preserve the existing pool.
    result = await mgr.refresh_from_text("not-a-proxy-at-all\n", fetch)
    assert result.preserved_previous is True
    assert mgr.proxy_count() == 1


def test_pick_proxy_returns_none_when_empty():
    mgr = SmartProxyManager()
    assert mgr.pick_proxy() is None


@pytest.mark.asyncio
async def test_pick_proxy_skips_excluded():
    mgr = SmartProxyManager(random_select=False)

    async def fetch(url):
        return ""

    await mgr.refresh_from_text("1.1.1.1:80\n2.2.2.2:80\n", fetch)
    picked = mgr.pick_proxy(excluded={"1.1.1.1:80"})
    assert picked == ("2.2.2.2:80", "http")


@pytest.mark.asyncio
async def test_block_proxy_bans_temporarily_then_recovers():
    mgr = SmartProxyManager(ban_time=1, random_select=False)

    async def fetch(url):
        return ""

    await mgr.refresh_from_text("1.1.1.1:80\n", fetch)
    assert mgr.pick_proxy() == ("1.1.1.1:80", "http")

    mgr.block_proxy("1.1.1.1:80", "HTTP 509")
    assert mgr.pick_proxy() is None  # banned right now
    assert mgr.proxy_count() == 1  # still in the pool, just banned
    assert mgr.count_blocked() == 1

    import time

    time.sleep(1.1)
    assert mgr.pick_proxy() == ("1.1.1.1:80", "http")  # ban expired


@pytest.mark.asyncio
async def test_block_proxy_with_zero_ban_time_removes_it():
    mgr = SmartProxyManager(ban_time=0, random_select=False)

    async def fetch(url):
        return ""

    await mgr.refresh_from_text("1.1.1.1:80\n2.2.2.2:80\n", fetch)
    mgr.block_proxy("1.1.1.1:80", "dead")
    assert mgr.proxy_count() == 1
    assert mgr.pick_proxy() == ("2.2.2.2:80", "http")


@pytest.mark.asyncio
async def test_auth_lookup():
    mgr = SmartProxyManager()

    async def fetch(url):
        return ""

    await mgr.refresh_from_text("1.1.1.1:80@dXNlcg==:cGFzcw==\n", fetch)
    assert mgr.get_auth("1.1.1.1:80") == "dXNlcg==:cGFzcw=="
    assert mgr.get_auth("2.2.2.2:80") is None


@pytest.mark.asyncio
async def test_build_proxy_url_without_auth():
    mgr = SmartProxyManager()

    async def fetch(url):
        return ""

    await mgr.refresh_from_text("1.1.1.1:80\n*2.2.2.2:1080\n", fetch)
    assert mgr.build_proxy_url("1.1.1.1:80", is_socks=False) == "http://1.1.1.1:80"
    assert mgr.build_proxy_url("2.2.2.2:1080", is_socks=True) == "socks5://2.2.2.2:1080"


def test_parse_proxy_list_addresses_skips_urls_and_malformed():
    text = "1.1.1.1:80\n*2.2.2.2:1080\n#https://example.com/list.txt\nnot-a-proxy\n\n"
    assert parse_proxy_list_addresses(text) == [("1.1.1.1:80", False), ("2.2.2.2:1080", True)]


@pytest.mark.asyncio
async def test_pick_proxy_prefers_verified_pool_over_configured_pool():
    mgr = SmartProxyManager(random_select=False)

    async def fetch(url):
        return ""

    await mgr.refresh_from_text("9.9.9.9:80\n", fetch)  # configured pool has an entry too
    mgr.verified_pool_provider = lambda: ["1.1.1.1:80", "*2.2.2.2:1080"]

    assert mgr.pick_proxy() == ("1.1.1.1:80", "http")


@pytest.mark.asyncio
async def test_pick_proxy_falls_back_to_configured_pool_when_verified_empty():
    mgr = SmartProxyManager(random_select=False)

    async def fetch(url):
        return ""

    await mgr.refresh_from_text("9.9.9.9:80\n", fetch)
    mgr.verified_pool_provider = lambda: []

    assert mgr.pick_proxy() == ("9.9.9.9:80", "http")


@pytest.mark.asyncio
async def test_pick_proxy_falls_back_when_all_verified_are_excluded_or_banned():
    mgr = SmartProxyManager(random_select=False)

    async def fetch(url):
        return ""

    await mgr.refresh_from_text("9.9.9.9:80\n", fetch)
    mgr.verified_pool_provider = lambda: ["1.1.1.1:80"]

    assert mgr.pick_proxy(excluded={"1.1.1.1:80"}) == ("9.9.9.9:80", "http")

    mgr.block_proxy("1.1.1.1:80", "HTTP 509")
    assert mgr.pick_proxy() == ("9.9.9.9:80", "http")


def test_block_proxy_on_verified_address_bans_it_without_touching_configured_pool():
    mgr = SmartProxyManager(ban_time=0, random_select=False)
    mgr.verified_pool_provider = lambda: ["1.1.1.1:80"]

    assert mgr.pick_proxy() == ("1.1.1.1:80", "http")
    mgr.block_proxy("1.1.1.1:80", "HTTP 509")
    assert mgr.pick_proxy() is None  # nothing else configured, verified one is now permanently banned
    assert mgr.proxy_count() == 0  # the configured pool itself was never touched


@pytest.mark.asyncio
async def test_build_proxy_url_decodes_base64_auth():
    import base64

    mgr = SmartProxyManager()
    user_b64 = base64.b64encode(b"bob").decode()
    pass_b64 = base64.b64encode(b"s3cr3t").decode()

    async def fetch(url):
        return ""

    await mgr.refresh_from_text(f"1.1.1.1:80@{user_b64}:{pass_b64}\n", fetch)
    assert mgr.build_proxy_url("1.1.1.1:80", is_socks=False) == "http://bob:s3cr3t@1.1.1.1:80"
