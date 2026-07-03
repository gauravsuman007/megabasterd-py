import json
import os

import httpx
import pytest

from app.core import crypto
from app.core.mega_api import MegaAPI
from app.features.folder_tree import build_tree, file_download_link, total_size


def _make_node(handle: str, parent: str, name: str, is_folder: bool, root_folder_key: bytes, size: int = 0) -> tuple[dict, bytes]:
    # Real MEGA file node keys are 32 bytes (obfuscated): the attribute is
    # encrypted with the 16-byte key derived by XORing the two halves, while
    # the stored/wrapped key keeps all 32 bytes. Folder keys are a plain 16
    # bytes. Using a 16-byte key for files here (as this helper used to) hid a
    # real bug where get_folder_nodes fed the raw 32-byte key to decrypt_attr.
    if is_folder:
        node_key = os.urandom(16)
        attr_key = node_key
    else:
        node_key = os.urandom(32)
        attr_key = crypto.init_mega_link_key(node_key)
    wrapped_key = crypto.encrypt_key(node_key, root_folder_key)
    attr = crypto.encrypt_attr(json.dumps({"n": name}).encode("utf-8"), attr_key)
    node = {
        "h": handle,
        "p": parent,
        "t": 1 if is_folder else 0,
        "k": f"{parent}:{crypto.bin_to_url_base64(wrapped_key)}",
        "a": crypto.bin_to_url_base64(attr),
    }
    if not is_folder:
        node["s"] = size
    return node, node_key


@pytest.mark.asyncio
async def test_get_folder_nodes_decrypts_flat_tree():
    root_folder_key = os.urandom(16)
    root_handle = "ROOT01"

    file1, file1_key = _make_node("FILE1", root_handle, "top-level.txt", is_folder=False, root_folder_key=root_folder_key, size=100)
    subfolder, _ = _make_node("SUB1", root_handle, "Season 1", is_folder=True, root_folder_key=root_folder_key)
    file2, file2_key = _make_node("FILE2", "SUB1", "episode.mkv", is_folder=False, root_folder_key=root_folder_key, size=5000)

    response_body = json.dumps([{"f": [file1, subfolder, file2]}])

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["n"] == root_handle
        return httpx.Response(200, text=response_body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = MegaAPI(client=client)

    folder_key_b64 = crypto.bin_to_url_base64(root_folder_key)
    nodes = await api.get_folder_nodes(root_handle, folder_key_b64)

    # Both files carry realistic 32-byte obfuscated keys; the old code fed
    # those straight to decrypt_attr (AES-256, garbage) and silently dropped
    # them, so a nested file like FILE2 would vanish and SUB1 would look empty.
    assert set(nodes.keys()) == {"FILE1", "SUB1", "FILE2"}
    assert nodes["FILE1"].name == "top-level.txt"
    assert nodes["FILE1"].size == 100
    assert nodes["FILE1"].parent == root_handle
    assert nodes["SUB1"].name == "Season 1"
    assert nodes["SUB1"].node_type == 1
    assert nodes["FILE2"].name == "episode.mkv"
    assert nodes["FILE2"].parent == "SUB1"
    assert nodes["FILE2"].size == 5000

    # Decrypted keys round-trip: each node's key must decrypt its own name.
    assert crypto.url_base64_to_bin(nodes["FILE1"].key) == file1_key
    assert crypto.url_base64_to_bin(nodes["FILE2"].key) == file2_key

    await client.aclose()


@pytest.mark.asyncio
async def test_get_folder_nodes_skips_undecryptable_node():
    root_folder_key = os.urandom(16)
    wrong_key = os.urandom(16)
    root_handle = "ROOT01"

    good_node, _ = _make_node("FILE1", root_handle, "good.txt", is_folder=False, root_folder_key=root_folder_key, size=1)
    # This node's key trailer is wrapped with the WRONG key, so no segment
    # will ever decrypt a valid attribute -- must be silently dropped.
    bad_node, _ = _make_node("FILE2", root_handle, "bad.txt", is_folder=False, root_folder_key=wrong_key, size=1)

    response_body = json.dumps([{"f": [good_node, bad_node]}])

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=response_body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = MegaAPI(client=client)

    nodes = await api.get_folder_nodes(root_handle, crypto.bin_to_url_base64(root_folder_key))
    assert set(nodes.keys()) == {"FILE1"}

    await client.aclose()


def test_build_tree_nests_by_parent_and_sorts_folders_first():
    from app.core.mega_api import FolderNode

    # "ROOT" is the share's own wrapper root node -- present in the
    # fetched set, with its own parent ("OWNER_ROOT") deliberately *not*
    # present, mirroring a real MEGA folder-link response (see
    # test_build_tree_root_parent_not_in_fetched_set_is_still_found for
    # why this matters: the public URL folder id is never actually a key
    # in `nodes`, only this internal wrapper's parent is missing).
    nodes = {
        "ROOT": FolderNode(handle="ROOT", node_type=1, parent="OWNER_ROOT", key="k0", name="Shared Folder", size=0),
        "F1": FolderNode(handle="F1", node_type=0, parent="ROOT", key="k1", name="zebra.txt", size=10),
        "D1": FolderNode(handle="D1", node_type=1, parent="ROOT", key="k2", name="alpha-folder", size=0),
        "F2": FolderNode(handle="F2", node_type=0, parent="D1", key="k3", name="nested.txt", size=20),
    }

    tree = build_tree(nodes)

    assert [n.name for n in tree] == ["alpha-folder", "zebra.txt"]  # folders sorted before files
    assert tree[0].is_folder is True
    assert len(tree[0].children) == 1
    assert tree[0].children[0].name == "nested.txt"
    assert tree[1].is_folder is False

    assert total_size(tree) == 30


def test_build_tree_root_parent_not_in_fetched_set_is_still_found():
    """Regression test for a real bug: MEGA's public folder id from the
    share URL is *never* a key in the fetched node set -- the actual
    top-level node's parent points to an internal/owner-side handle
    that's outside what a public folder link can see. Matching against
    the URL's folder id (the old behavior) silently found nothing and
    produced an empty tree; build_tree must find the root structurally
    instead."""
    from app.core.mega_api import FolderNode

    nodes = {
        "kdtnmKaT": FolderNode(handle="kdtnmKaT", node_type=1, parent="JJsQFQoY", key="k0", name="Shared Folder", size=0),
        "FILE1": FolderNode(handle="FILE1", node_type=0, parent="kdtnmKaT", key="k1", name="video.mp4", size=123),
    }

    tree = build_tree(nodes)

    assert [n.name for n in tree] == ["video.mp4"]


def test_build_tree_multiple_unrooted_nodes_are_shown_as_top_level():
    from app.core.mega_api import FolderNode

    # No single wrapper node -- two files both reference a parent that's
    # not in the fetched set at all. Unexpected shape, but must not just
    # silently disappear into an empty tree.
    nodes = {
        "F1": FolderNode(handle="F1", node_type=0, parent="MISSING", key="k1", name="a.txt", size=1),
        "F2": FolderNode(handle="F2", node_type=0, parent="MISSING", key="k2", name="b.txt", size=2),
    }

    tree = build_tree(nodes)

    assert {n.name for n in tree} == {"a.txt", "b.txt"}


def test_file_download_link_builds_scoped_link():
    from app.features.folder_tree import TreeNode

    node = TreeNode(handle="FILE1", name="clip.mp4", is_folder=False, size=1, key="somekey")
    link = file_download_link(node, folder_id="ROOT01")
    assert link == "https://mega.nz/#N!FILE1!somekey###n=ROOT01"


def test_file_download_link_rejects_folder_node():
    from app.features.folder_tree import TreeNode

    node = TreeNode(handle="D1", name="folder", is_folder=True, size=0, key="k")
    with pytest.raises(ValueError):
        file_download_link(node, folder_id="ROOT01")


async def _no_network(request: httpx.Request) -> httpx.Response:
    raise AssertionError("should not make a network call for a folder link -- 'a:g' expects a file handle")


@pytest.mark.asyncio
async def test_get_mega_file_metadata_rejects_folder_link():
    """Regression test: passing a folder link straight to the file-metadata
    call used to send MEGA's public folder handle as if it were a file
    handle, which MEGA rejects with a cryptic -11 (Access denied) instead
    of a message pointing at the actual mistake."""
    api = MegaAPI(client=httpx.AsyncClient(transport=httpx.MockTransport(_no_network)))
    with pytest.raises(ValueError, match="folder link"):
        await api.get_mega_file_metadata("https://mega.nz/folder/RAs0FQhJ#XrCLS3_t_9PFHxhrr0ocNw")
    await api.aclose()


@pytest.mark.asyncio
async def test_get_mega_file_download_url_rejects_folder_link():
    api = MegaAPI(client=httpx.AsyncClient(transport=httpx.MockTransport(_no_network)))
    with pytest.raises(ValueError, match="folder link"):
        await api.get_mega_file_download_url("https://mega.nz/folder/RAs0FQhJ#XrCLS3_t_9PFHxhrr0ocNw")
    await api.aclose()


@pytest.mark.asyncio
async def test_browse_folder_route_never_proxies_the_listing_lookup():
    """Regression test: folder browsing is a metadata lookup, not a byte
    transfer -- it must never route through SmartProxy even when it's
    enabled, since a bad/slow proxy would otherwise break the folder
    picker for everyone (only Downloader/Uploader chunk transfer should
    be proxied)."""
    from unittest.mock import AsyncMock, patch

    from app import state
    from app.api.routes_folder import browse_folder

    original_enabled = state.smart_proxy_enabled
    state.smart_proxy_enabled = True

    fake_api = AsyncMock()
    fake_api.get_folder_nodes.return_value = {}

    try:
        with patch("app.api.routes_folder.MegaAPI", return_value=fake_api) as mock_mega_api:
            await browse_folder(link="https://mega.nz/folder/RAs0FQhJ#XrCLS3_t_9PFHxhrr0ocNw")
        assert mock_mega_api.call_args.kwargs["proxy_manager"] is None
    finally:
        state.smart_proxy_enabled = original_enabled
