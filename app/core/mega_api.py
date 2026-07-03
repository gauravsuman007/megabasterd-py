"""Async MEGA API client, ported from MegaAPI.java.

Talks to MEGA's `/cs` JSON-RPC endpoint. One instance per logged-in session
(or per anonymous/public-link session with no login at all).
"""
from __future__ import annotations

import asyncio
import json
import re
import secrets
import string
from dataclasses import dataclass

import httpx

from app.core import crypto, hashcash
from app.core.errors import MegaAPIException
from app.core.link_parser import parse_mega_link
from app.core.proxy_manager import SmartProxyManager

API_URL = "https://g.api.mega.co.nz"
DEFAULT_APP_KEY = "BdARkQSQ"
MAX_RAW_REQUEST_RETRIES = 30
MEGA_ERROR_NO_EXCEPTION_CODES = {-1, -3}
PBKDF2_ITERATIONS = 100_000

EXP_BACKOFF_BASE = 2
EXP_BACKOFF_SECS_RETRY = 1
EXP_BACKOFF_MAX_WAIT_TIME = 8

_ID_CHARS = string.ascii_letters + string.digits
_BARE_ERROR_RE = re.compile(r"^\[?(-[0-9]+)\]?$")


def _gen_id(length: int) -> str:
    return "".join(secrets.choice(_ID_CHARS) for _ in range(length))


def wait_time_exp_backoff(retry_count: int) -> int:
    wait = (EXP_BACKOFF_BASE**retry_count) * EXP_BACKOFF_SECS_RETRY
    return min(wait, EXP_BACKOFF_MAX_WAIT_TIME)


def check_mega_error(data: str) -> int:
    m = _BARE_ERROR_RE.match(data.strip())
    return int(m.group(1)) if m else 0


@dataclass
class Quota:
    used_storage: int
    max_storage: int


@dataclass
class FolderNode:
    handle: str
    node_type: int  # 0 = file, 1 = folder
    parent: str | None
    key: str  # decrypted node key, url-base64
    name: str
    size: int


@dataclass
class FileMetadata:
    name: str
    size: int
    file_key: str  # base64url, as it appeared in the link


class MegaAPI:
    """One MEGA session: login state + JSON-RPC request/response handling.

    Not thread-safe across concurrent event loops sharing one instance is
    fine (asyncio, single-threaded); `seqno` increments are not protected by
    a lock since there is no `await` between read and increment.
    """

    def __init__(
        self,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
        lang: str | None = None,
        proxy_manager: SmartProxyManager | None = None,
    ):
        self.api_key = api_key or DEFAULT_APP_KEY
        self.lang = lang
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = client is None
        self.proxy_manager = proxy_manager

        self.req_id = _gen_id(10)
        self._seqno = secrets.randbelow(2**32)

        self.sid: str | None = None
        self.email: str | None = None
        self.full_email: str | None = None
        self.account_version: int = -1
        self.salt: bytes | None = None
        self.password_aes: list[int] | None = None
        self.user_hash: str | None = None
        self.master_key: bytes | None = None
        self.rsa_priv: crypto.RSAPrivateComponents | None = None

        self.root_id: str | None = None
        self.inbox_id: str | None = None
        self.trashbin_id: str | None = None

        self.last_api_error_code: int = 0

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _next_seqno(self) -> str:
        seq = self._seqno
        self._seqno += 1
        return str(seq)

    def _std_params(self) -> dict[str, str]:
        params = {"ak": self.api_key}
        if self.lang:
            params["lang"] = self.lang
        return params

    async def _raw_request(self, commands: list[dict], extra_params: dict | None = None) -> list:
        """POST one or more command objects to /cs and return the parsed
        JSON response array. Retries on transient HTTP errors, MEGA's
        hashcash proof-of-work challenge (402), and the -1/-3 "retry me"
        error codes; raises MegaAPIException for anything else."""
        params = {"id": None, **self._std_params()}
        if self.sid:
            params["sid"] = self.sid
        if extra_params:
            params.update(extra_params)

        body = json.dumps(commands).encode("utf-8")

        pending_hashcash: str | None = None
        conta_error = 0
        response_text: str | None = None

        # SmartProxy routing state: only engaged on HTTP 509 (bandwidth
        # quota), and only if a proxy_manager was configured -- mirrors
        # Java's `http_error == 509 && isUse_smart_proxy()` gate.
        proxy_client: httpx.AsyncClient | None = None
        proxy_address: str | None = None
        excluded_proxies: set[str] = set()

        try:
            while True:
                params["id"] = self._next_seqno()
                headers = {"Content-Type": "text/plain;charset=UTF-8", "User-Agent": "MegaBasterd-Py/0.1"}
                if pending_hashcash:
                    headers["X-Hashcash"] = pending_hashcash
                    pending_hashcash = None

                http_error = 0
                empty_response = False
                mega_error = 0
                hashcash_just_solved = False

                client = proxy_client or self._client
                try:
                    resp = await client.post(f"{API_URL}/cs", params=params, content=body, headers=headers)
                except httpx.HTTPError:
                    empty_response = True
                    if proxy_client is not None and proxy_address is not None:
                        self.proxy_manager.block_proxy(proxy_address, "connection failed")
                else:
                    if resp.status_code != 200:
                        http_error = resp.status_code
                        if resp.status_code == 402:
                            challenge = resp.headers.get("X-Hashcash")
                            if challenge:
                                try:
                                    pending_hashcash = hashcash.solve(challenge)
                                    hashcash_just_solved = True
                                except Exception:
                                    pass
                    else:
                        response_text = resp.text
                        if response_text:
                            mega_error = check_mega_error(response_text)
                            if mega_error != 0:
                                self.last_api_error_code = mega_error
                                if mega_error not in MEGA_ERROR_NO_EXCEPTION_CODES:
                                    raise MegaAPIException(mega_error)
                        else:
                            empty_response = True

                if not (empty_response or mega_error != 0 or http_error != 0):
                    break

                smart_proxy_retry = http_error == 509 and self.proxy_manager is not None
                is_retryable_state = http_error in (402, 500, 503) or empty_response or mega_error != 0 or smart_proxy_retry
                if not is_retryable_state or conta_error >= MAX_RAW_REQUEST_RETRIES:
                    break

                if smart_proxy_retry:
                    if proxy_address is not None:
                        self.proxy_manager.block_proxy(proxy_address, "HTTP 509")
                        excluded_proxies.add(proxy_address)
                    picked = self.proxy_manager.pick_proxy(excluded_proxies)
                    if picked is not None:
                        proxy_address, proxy_type = picked
                        proxy_url = self.proxy_manager.build_proxy_url(proxy_address, proxy_type == "socks")
                        if proxy_client is not None:
                            await proxy_client.aclose()
                        proxy_client = httpx.AsyncClient(proxy=proxy_url, timeout=self.proxy_manager.proxy_timeout)

                if not hashcash_just_solved:
                    await asyncio.sleep(wait_time_exp_backoff(conta_error))
                    conta_error += 1
                else:
                    await asyncio.sleep(0)  # yield, retry immediately, no backoff, no counter bump
        finally:
            if proxy_client is not None:
                await proxy_client.aclose()

        if response_text is None:
            raise MegaAPIException(-1, "no response from MEGA API after retries")

        return json.loads(response_text)

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    async def read_account_version_and_salt(self, email: str) -> None:
        res = await self._raw_request([{"a": "us0", "user": email}])
        self.account_version = res[0].get("v", 1)
        salt_b64 = res[0].get("s")
        self.salt = crypto.url_base64_to_bin(salt_b64) if salt_b64 else None

    async def check_2fa(self, email: str) -> bool:
        email = email.split("#")[0].strip()
        res = await self._raw_request([{"a": "mfag", "e": email}])
        return res[0] == 1

    async def login(self, email: str, password: str, pincode: str | None = None) -> None:
        self.full_email = email
        self.email = email.split("#")[0].strip()

        if self.account_version == -1:
            await self.read_account_version_and_salt(self.email)

        if self.account_version == 1:
            self.password_aes = crypto.mega_prepare_master_key_v1(password.encode("utf-8"))
            self.user_hash = crypto.mega_user_hash(self.email, self.password_aes)
        else:
            self.password_aes, self.user_hash = crypto.derive_login_key_v2(password, self.salt or b"")

        await self._real_login(pincode)

    async def fast_login(self, email: str, password_aes: list[int], user_hash: str, pincode: str | None = None) -> None:
        self.full_email = email
        self.email = email.split("#")[0].strip()
        if self.account_version == -1:
            await self.read_account_version_and_salt(self.email)
        self.password_aes = password_aes
        self.user_hash = user_hash
        await self._real_login(pincode)

    async def _real_login(self, pincode: str | None) -> None:
        cmd: dict = {"a": "us", "user": self.email, "uh": self.user_hash}
        if pincode:
            cmd["mfa"] = pincode

        res = await self._raw_request([cmd])
        node = res[0]

        k = node.get("k")
        privk = node.get("privk")
        csid = node.get("csid")
        if not (k and privk and csid):
            raise MegaAPIException(-1, "MEGA `us` response missing k/privk/csid")

        password_aes_bytes = crypto.i32a2bin(self.password_aes)
        self.master_key = crypto.decrypt_key(crypto.url_base64_to_bin(k), password_aes_bytes)

        privk_bytes = crypto.decrypt_key(crypto.url_base64_to_bin(privk), self.master_key)
        self.rsa_priv = crypto.parse_rsa_privk(privk_bytes)

        raw_sid = crypto.rsa_decrypt_csid(crypto.url_base64_to_bin(csid), self.rsa_priv)
        self.sid = crypto.bin_to_url_base64(raw_sid[:43])

        await self.fetch_nodes()

    # ------------------------------------------------------------------
    # Account / tree
    # ------------------------------------------------------------------

    async def fetch_nodes(self) -> None:
        res = await self._raw_request([{"a": "f", "c": 1}])
        for element in res[0].get("f", []):
            file_type = element.get("t")
            if file_type == 2:
                self.root_id = element.get("h")
            elif file_type == 3:
                self.inbox_id = element.get("h")
            elif file_type == 4:
                self.trashbin_id = element.get("h")

    async def get_quota(self) -> Quota | None:
        res = await self._raw_request([{"a": "uq", "xfer": 1, "strg": 1}])
        node = res[0]
        if "cstrg" not in node or "mstrg" not in node:
            return None
        return Quota(used_storage=int(node["cstrg"]), max_storage=int(node["mstrg"]))

    # ------------------------------------------------------------------
    # Public link metadata / download
    # ------------------------------------------------------------------

    async def get_mega_file_metadata(self, link: str) -> FileMetadata:
        parsed = parse_mega_link(link)
        if parsed.kind == "folder":
            raise ValueError("This is a folder link, not a file link -- browse it to pick an individual file to download.")
        cmd: dict = {"a": "g", "p": parsed.handle}
        extra_params = None
        if parsed.folder_id:
            cmd = {"a": "g", "n": parsed.handle}
            extra_params = {"n": parsed.folder_id}

        res = await self._raw_request([cmd], extra_params)
        node = res[0]
        size = int(node["s"])
        at = node["at"]

        file_key_bytes = crypto.url_base64_to_bin(parsed.key)
        aes_key = crypto.init_mega_link_key(file_key_bytes)
        attr = crypto.decrypt_attr(crypto.url_base64_to_bin(at), aes_key)
        name = json.loads(attr)["n"]

        return FileMetadata(name=name, size=size, file_key=parsed.key)

    async def get_mega_file_download_url(self, link: str) -> str:
        parsed = parse_mega_link(link)
        if parsed.kind == "folder":
            raise ValueError("This is a folder link, not a file link -- browse it to pick an individual file to download.")
        cmd: dict = {"a": "g", "g": "1", "p": parsed.handle}
        extra_params = None
        if parsed.folder_id:
            cmd = {"a": "g", "g": "1", "n": parsed.handle}
            extra_params = {"n": parsed.folder_id}

        res = await self._raw_request([cmd], extra_params)
        node = res[0]
        download_url = node.get("g")
        if not download_url:
            raise MegaAPIException(-101, "MEGA did not return a download URL")
        return download_url

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------

    async def init_upload_file(self, file_size: int) -> str:
        res = await self._raw_request([{"a": "u", "s": file_size}])
        return res[0]["p"]

    async def finish_upload_file(
        self,
        file_basename: str,
        ul_key_words: list[int],
        fkey_words: list[int],
        completion_handle: str,
        mega_parent: str,
        master_key: bytes,
        root_node: str,
        share_key: bytes | None = None,
    ) -> dict:
        """`ul_key_words` is the real (un-obfuscated) 4-word AES key generated
        for this upload -- used to encrypt the filename attribute. `fkey_words`
        is the 8-word obfuscated node key (see transfers/upload.py's
        build_node_key) that gets wrapped with master_key/share_key for
        storage. Mixing these up (encrypting the attribute with the
        obfuscated key) silently produces an undecryptable filename."""
        enc_att = crypto.encrypt_attr(json.dumps({"n": file_basename}).encode("utf-8"), crypto.i32a2bin(ul_key_words[:4]))
        enc_node_key = crypto.encrypt_key(crypto.i32a2bin(fkey_words), master_key)
        share_key_for_cr = share_key if share_key is not None else master_key
        enc_node_key_share = crypto.encrypt_key(crypto.i32a2bin(fkey_words), share_key_for_cr)

        cmd = {
            "a": "p",
            "t": mega_parent,
            "n": [
                {
                    "h": completion_handle,
                    "t": 0,
                    "a": crypto.bin_to_url_base64(enc_att),
                    "k": crypto.bin_to_url_base64(enc_node_key),
                }
            ],
            "i": self.req_id,
            "cr": [[root_node], [completion_handle], [0, 0, crypto.bin_to_url_base64(enc_node_key_share)]],
        }
        res = await self._raw_request([cmd])
        return res[0]

    async def create_dir(self, name: str, parent_node: str, node_key: bytes, master_key: bytes) -> dict:
        enc_att = crypto.encrypt_attr(json.dumps({"n": name}).encode("utf-8"), node_key)
        enc_node_key = crypto.encrypt_key(node_key, master_key)
        cmd = {
            "a": "p",
            "t": parent_node,
            "n": [{"h": "xxxxxxxx", "t": 1, "a": crypto.bin_to_url_base64(enc_att), "k": crypto.bin_to_url_base64(enc_node_key)}],
            "i": self.req_id,
        }
        res = await self._raw_request([cmd])
        return res[0]

    async def get_public_file_link(self, node: str, node_key: bytes) -> str:
        res = await self._raw_request([{"a": "l", "n": node}])
        file_id = res[0]
        return f"https://mega.nz/#!{file_id}!{crypto.bin_to_url_base64(node_key)}"

    async def get_public_folder_link(self, node: str, node_key: bytes) -> str:
        res = await self._raw_request([{"a": "l", "n": node, "i": self.req_id}])
        folder_id = res[0]
        return f"https://mega.nz/#F!{folder_id}!{crypto.bin_to_url_base64(node_key)}"

    async def get_folder_nodes(self, folder_id: str, folder_key: str) -> dict[str, FolderNode]:
        """Fetch and decrypt every node in a public folder link, ported
        from MegaAPI.getFolderNodes. Returns a flat handle -> FolderNode
        map (parent/child structure via each node's `parent` field, not a
        nested tree) -- see app.features.folder_tree for building the
        actual tree.

        Each node's `k` field is one or more '/'-separated
        `handle:base64key` segments (multiple when the folder involves
        several sharing contexts); MEGA doesn't say which segment applies
        to this node, so every segment is trial-decrypted with the
        folder's own key until one produces an attribute blob that
        actually decodes to a name ("Yellowstone" trial-decrypt loop in
        the original, named after the GitHub issue that added it).
        """
        res = await self._raw_request([{"a": "f", "c": "1", "r": "1", "ca": "1"}], extra_params={"n": folder_id})
        folder_entries = res[0].get("f", [])

        decoded_folder_key = crypto.url_base64_to_bin(folder_key)
        nodes: dict[str, FolderNode] = {}

        for node in folder_entries:
            full_k = node.get("k")
            if not full_k:
                continue

            valid_key: str | None = None
            valid_attr: dict | None = None

            for segment in full_k.split("/"):
                parts = segment.split(":")
                if len(parts) < 2:
                    continue
                potential_key_b64 = parts[-1]
                try:
                    node_key_bin = crypto.url_base64_to_bin(potential_key_b64)
                    decrypted_key_bin = crypto.decrypt_key(node_key_bin, decoded_folder_key)
                    dec_node_k = crypto.bin_to_url_base64(decrypted_key_bin)

                    # File node keys are 32 bytes (obfuscated); the attribute is
                    # encrypted with the 16-byte AES key derived by XORing the two
                    # halves (init_mega_link_key). Folder keys are already 16 bytes
                    # and pass through unchanged. Feeding the raw 32-byte key to
                    # decrypt_attr silently runs AES-256, produces garbage, and the
                    # node gets dropped -- which is why nested files vanished and
                    # their folders looked empty. The stored `valid_key` keeps the
                    # full obfuscated key, since download links need all 32 bytes.
                    attr_key = crypto.init_mega_link_key(decrypted_key_bin)
                    attr = crypto.decrypt_attr(crypto.url_base64_to_bin(node["a"]), attr_key)
                    parsed_attr = json.loads(attr)
                    if parsed_attr.get("n"):
                        valid_key = dec_node_k
                        valid_attr = parsed_attr
                        break
                except Exception:
                    continue

            if valid_attr is None:
                continue

            handle = node["h"]
            nodes[handle] = FolderNode(
                handle=handle,
                node_type=node.get("t", 0),
                parent=node.get("p"),
                key=valid_key,
                name=valid_attr["n"],
                size=int(node["s"]) if node.get("s") is not None else 0,
            )

        return nodes
