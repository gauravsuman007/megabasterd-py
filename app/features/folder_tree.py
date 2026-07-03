"""Build a nested folder tree from MegaAPI.get_folder_nodes' flat
handle -> FolderNode map, ported from the tree-building half of
FolderLinkDialog.java (MegaAPI itself only returns the flat map; Java's
Swing JTree model did the nesting client-side, same split kept here).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.core.link_parser import build_scoped_file_link
from app.core.mega_api import FolderNode


@dataclass
class TreeNode:
    """One node in the nested folder tree: its handle/name/key, whether it's a
    folder, its `size` (0 for folders), and its `children` (empty for files)."""
    handle: str
    name: str
    is_folder: bool
    size: int
    key: str
    children: list[TreeNode] = field(default_factory=list)


def build_tree(nodes: dict[str, FolderNode]) -> list[TreeNode]:
    """Returns the shared folder's contents as a nested tree.

    MEGA stamps each top-level node's `parent` with an internal
    owner-side handle -- it is *not* the public folder id from the share
    URL, and that handle never appears in `nodes` at all (it's outside
    what a public folder link can see). So instead of matching against a
    caller-supplied root handle (which would silently match nothing and
    produce an empty tree), the real root is found structurally: the
    node(s) whose parent isn't itself among the fetched nodes."""
    by_parent: dict[str, list[FolderNode]] = {}
    for node in nodes.values():
        by_parent.setdefault(node.parent, []).append(node)

    def build(handle: str) -> list[TreeNode]:
        children = by_parent.get(handle, [])
        ordered = sorted(children, key=lambda n: (n.node_type != 1, n.name.lower()))
        return [
            TreeNode(
                handle=child.handle,
                name=child.name,
                is_folder=child.node_type == 1,
                size=child.size,
                key=child.key,
                children=build(child.handle) if child.node_type == 1 else [],
            )
            for child in ordered
        ]

    roots = [handle for handle, node in nodes.items() if node.parent not in nodes]

    if len(roots) == 1:
        # The normal case: one wrapper root node for the share -- return
        # its contents, not the wrapper itself (callers already know the
        # folder they browsed).
        return build(roots[0])

    # Zero or multiple "no parent in this fetch" nodes is unexpected, but
    # rather than showing an empty tree, treat each one as a top-level
    # entry directly.
    ordered_roots = sorted((nodes[h] for h in roots), key=lambda n: (n.node_type != 1, n.name.lower()))
    return [
        TreeNode(
            handle=node.handle,
            name=node.name,
            is_folder=node.node_type == 1,
            size=node.size,
            key=node.key,
            children=build(node.handle) if node.node_type == 1 else [],
        )
        for node in ordered_roots
    ]


def file_download_link(node: TreeNode, folder_id: str) -> str:
    """Build the folder-scoped download link for a file node inside the public
    folder `folder_id`. Raises ValueError if `node` is a folder."""
    if node.is_folder:
        raise ValueError(f"{node.name!r} is a folder, not a file")
    return build_scoped_file_link(node.handle, node.key, folder_id)


def total_size(tree: list[TreeNode]) -> int:
    """Sum of every file's size in the tree, recursing into folders."""
    total = 0
    for node in tree:
        total += node.size if not node.is_folder else total_size(node.children)
    return total
