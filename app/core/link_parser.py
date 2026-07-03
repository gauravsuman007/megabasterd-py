"""MEGA link parsing, ported from MiscTools.newMegaLinks2Legacy + the regexes
used in MegaAPI.getMegaFileMetadata/getMegaFileDownloadUrl.

MEGA has shipped two URL shapes over the years:
  legacy:  https://mega.nz/#!<id>!<key>            (file)
           https://mega.nz/#F!<id>!<key>           (folder)
  modern:  https://mega.nz/file/<id>#<key>
           https://mega.nz/folder/<id>#<key>[/file/<file_id>]

We normalize both into a single dataclass. Folder-scoped file links (a file
inside an already-browsed public folder) carry an extra `folder_id` used as
the `&n=` query parameter on the `g` (download) API call — full folder-tree
browsing is a later phase, but the parsing/shape is defined now so the
download-URL/metadata calls can already handle links produced by it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class MegaLink:
    kind: str  # "file" or "folder"
    handle: str
    key: str
    folder_id: str | None = None  # set when this is a file inside a public folder


_FOLDER_FILE = re.compile(r"(?:https://)?mega(?:\.co)?\.nz/folder/([^#]+)#([^\r\n/]+)/file/([^\r\n/]+)")
_FOLDER_SUBFOLDER = re.compile(r"(?:https://)?mega(?:\.co)?\.nz/folder/([^#]+)#([^\r\n/]+)/folder/([^\r\n/]+)")
_FOLDER = re.compile(r"(?:https://)?mega(?:\.co)?\.nz/folder/([^#]+)#([^\r\n]+)")
_FILE = re.compile(r"(?:https://)?mega(?:\.co)?\.nz/file/([^#]+)#([^\r\n]+)")

_LEGACY_FOLDER_FILE = re.compile(r"#F\*([^!]+)!([^!]+)!([^!#]+)")
_LEGACY_FOLDER_SUBFOLDER = re.compile(r"#F!([^!@]+)@([^!]+)!([^!#]+)")
_LEGACY_FOLDER = re.compile(r"#F!([^!]+)!([^!#]+)")
_LEGACY_FILE = re.compile(r"#!([^!]+)!([^!#]+)")
_LEGACY_FOLDER_SCOPED_FILE = re.compile(r"#N!([^!]+)!([^!#]+)###n=(.+)$")


def new_links_to_legacy(data: str) -> str:
    """Port of MiscTools.newMegaLinks2Legacy: rewrite modern mega.nz/file
    and mega.nz/folder URLs into the legacy #!/#F! fragment form."""
    data = _FOLDER_FILE.sub(r"https://mega.nz/#F*\3!\1!\2", data)
    data = _FOLDER_SUBFOLDER.sub(r"https://mega.nz/#F!\1@\3!\2", data)
    data = _FOLDER.sub(r"https://mega.nz/#F!\1!\2", data)
    data = _FILE.sub(r"https://mega.nz/#!\1!\2", data)
    return data


def parse_mega_link(link: str) -> MegaLink:
    link = new_links_to_legacy(link.strip())

    if m := _LEGACY_FOLDER_SCOPED_FILE.search(link):
        return MegaLink(kind="file", handle=m.group(1), key=m.group(2), folder_id=m.group(3))
    if m := _LEGACY_FOLDER_FILE.search(link):
        return MegaLink(kind="file", handle=m.group(1), key=m.group(3), folder_id=m.group(2))
    if m := _LEGACY_FOLDER.search(link):
        return MegaLink(kind="folder", handle=m.group(1), key=m.group(2))
    if m := _LEGACY_FILE.search(link):
        return MegaLink(kind="file", handle=m.group(1), key=m.group(2))

    raise ValueError(f"Unrecognized MEGA link: {link!r}")


def build_scoped_file_link(file_handle: str, file_key: str, folder_id: str) -> str:
    """Build the internal '#N!handle!key###n=folder_id' representation for a
    file discovered while browsing a public folder link."""
    return f"https://mega.nz/#N!{file_handle}!{file_key}###n={folder_id}"
