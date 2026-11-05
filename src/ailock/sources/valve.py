"""Valve replay CDN: match metadata blobs and demos.

URLs need the match's cluster id and salts, which only the Game Coordinator
hands out (we get them from deadlock-api). Valve expires these files after a
while, so download them as soon as a match is seen.
"""
import bz2

import httpx
import zstandard

from ..proto import cm


def replay_url(match_id: int, cluster_id: int, salt: int, ext: str) -> str:
    return f"http://replay{cluster_id}.valve.net/1422450/{match_id}_{salt}.{ext}"


def download(url: str, timeout: float = 600.0) -> bytes:
    with httpx.Client(timeout=timeout, follow_redirects=True) as http:
        r = http.get(url)
        r.raise_for_status()
        return r.content


def decompress(blob: bytes) -> bytes:
    # files are named .bz2 but are currently zstd; support both
    if blob[:4] == b"\x28\xb5\x2f\xfd":
        return zstandard.ZstdDecompressor().decompress(blob, max_output_size=1 << 30)
    if blob[:3] == b"BZh":
        return bz2.decompress(blob)
    return blob


def parse_metadata(blob: bytes) -> "cm.CMsgMatchMetaDataContents":
    outer = cm.CMsgMatchMetaData()
    outer.ParseFromString(decompress(blob))
    contents = cm.CMsgMatchMetaDataContents()
    contents.ParseFromString(outer.match_details)
    return contents
