from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException

from app import state
from app.core.link_parser import parse_mega_link
from app.core.mega_api import MegaAPI
from app.features.folder_tree import build_tree, file_download_link

router = APIRouter(prefix="/api", tags=["folder"])


def _tree_to_dict(nodes, folder_id: str) -> list[dict]:
    out = []
    for node in nodes:
        d = asdict(node)
        d["children"] = _tree_to_dict(node.children, folder_id)
        if not node.is_folder:
            d["download_link"] = file_download_link(node, folder_id=folder_id)
        out.append(d)
    return out


@router.get("/folder")
async def browse_folder(link: str):
    parsed = parse_mega_link(link)
    if parsed.kind != "folder":
        raise HTTPException(400, "Not a folder link")

    # No proxy here: this is a metadata/listing lookup, not a byte
    # transfer -- only Downloader/Uploader route actual chunk fetches
    # through SmartProxy (see routes_transfers._run_download).
    api = MegaAPI(api_key=state.mega_api_key, proxy_manager=None)
    try:
        nodes = await api.get_folder_nodes(parsed.handle, parsed.key)
    finally:
        await api.aclose()

    tree = build_tree(nodes)
    return {"folder_id": parsed.handle, "tree": _tree_to_dict(tree, parsed.handle)}
