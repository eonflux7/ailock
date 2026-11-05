"""Minimal Source 2 demo reader: frames -> packets -> net/user messages.

Enough to pull embedded messages (e.g. the post-match details the server broadcasts at the end, which carry the
same CMsgMatchMetaDataContents as Valve's .meta file). Entity state isn't decoded here.

Format: "PBDEMS2\\0" + two int32 offsets, then frames of varint(cmd | 64 if snappy-compressed), varint(tick),
varint(size), payload. DEM_Packet/SignonPacket/FullPacket payloads hold a bitstream of
(ubitvar type, varint size, bytes) messages.
"""
import csv
import io
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

import cramjam

from .proto import demo as dm

MAGIC = b"PBDEMS2\0"
COMPRESSED = 64
PACKET_CMDS = {dm.DEM_Packet, dm.DEM_SignonPacket}
POST_MATCH_DETAILS = 316  # k_EUserMsg_PostMatchDetails (citadel_usermessages.proto)


def _varint(f: BinaryIO) -> int | None:
    out = shift = 0
    while True:
        b = f.read(1)
        if not b:
            return None
        out |= (b[0] & 0x7F) << shift
        if not b[0] & 0x80:
            return out
        shift += 7


def frames(path: Path) -> Iterator[tuple[int, int, bytes]]:
    """Yield (cmd, tick, payload) for every frame, payloads decompressed."""
    with open(path, "rb") as f:
        if f.read(8) != MAGIC:
            raise ValueError(f"{path}: not a Source 2 demo")
        f.read(8)
        while (cmd := _varint(f)) is not None:
            tick, size = _varint(f), _varint(f)
            data = f.read(size)
            if cmd & COMPRESSED:
                data = bytes(cramjam.snappy.decompress_raw(data))
            yield cmd & ~COMPRESSED, tick, data


class _Bits:
    def __init__(self, data: bytes):
        self.n = int.from_bytes(data, "little")
        self.pos, self.end = 0, len(data) * 8

    def read(self, bits: int) -> int:
        v = (self.n >> self.pos) & ((1 << bits) - 1)
        self.pos += bits
        return v

    def ubitvar(self) -> int:
        v = self.read(6)
        tail = v & 0x30
        if tail == 16:
            return (v & 15) | (self.read(4) << 4)
        if tail == 32:
            return (v & 15) | (self.read(8) << 4)
        if tail == 48:
            return (v & 15) | (self.read(28) << 4)
        return v

    def varint(self) -> int:
        out = shift = 0
        while True:
            b = self.read(8)
            out |= (b & 0x7F) << shift
            if not b & 0x80:
                return out
            shift += 7

    def bytes(self, n: int) -> bytes:
        return self.read(n * 8).to_bytes(n, "little")


def messages(packet_data: bytes) -> Iterator[tuple[int, bytes]]:
    """(message type, payload) pairs from a CDemoPacket's data bitstream."""
    b = _Bits(packet_data)
    while b.end - b.pos >= 8:
        kind = b.ubitvar()
        size = b.varint()
        if b.pos + size * 8 > b.end:
            return
        yield kind, b.bytes(size)


def header(path: Path) -> "dm.CDemoFileHeader":
    for cmd, _, data in frames(path):
        if cmd == dm.DEM_FileHeader:
            h = dm.CDemoFileHeader()
            h.ParseFromString(data)
            return h
    raise ValueError(f"{path}: no file header")


def post_match_details(path: Path, tail_frames: int = 20000) -> bytes | None:
    """Raw CMsgMatchMetaDataContents bytes from the post-match broadcast, if the demo recorded one.

    The message is sent at the very end, so only the last `tail_frames` packets are bit-decoded.
    """
    recent: list[bytes] = []
    for cmd, _, data in frames(path):
        if cmd in PACKET_CMDS:
            recent.append(data)
            if len(recent) > tail_frames:
                recent.pop(0)
    for data in reversed(recent):
        p = dm.CDemoPacket()
        p.ParseFromString(data)
        for kind, payload in messages(p.data):
            if kind == POST_MATCH_DETAILS:
                from ._proto import citadel_usermessages_pb2 as um
                msg = um.CCitadelUserMsg_PostMatchDetails()
                msg.ParseFromString(payload)
                return msg.match_details
    return None


# Rust extractor (parser/, built on haste): cargo build --release --manifest-path parser/Cargo.toml
EXTRACTOR = Path(__file__).resolve().parents[2] / "parser" / "target" / "release" / "ailock-demo"
# demo world coords are offset from Valve's match metadata coords (verified against death positions)
COORD_OFFSET = 16384.0


def positions(path: Path, hz: float = 1.0) -> list[dict]:
    """Per-player samples from the demo: game_time_s, account_id, hero_id, team, health, max_health, alive, x, y, z."""
    if not EXTRACTOR.exists():
        raise FileNotFoundError(f"{EXTRACTOR} not built: cargo build --release --manifest-path parser/Cargo.toml")
    out = subprocess.run([str(EXTRACTOR), str(path), str(hz)], capture_output=True, text=True, check=True).stdout
    rows = []
    for r in csv.DictReader(io.StringIO(out)):
        rows.append({
            "game_time_s": float(r["game_time_s"]), "account_id": int(r["account_id"]), "hero_id": int(r["hero_id"]),
            "team": int(r["team"]), "health": int(r["health"]), "max_health": int(r["max_health"]),
            "alive": r["alive"] == "1", "x": float(r["x"]) - COORD_OFFSET, "y": float(r["y"]) - COORD_OFFSET,
            "z": float(r["z"]) - COORD_OFFSET,
        })
    return rows
