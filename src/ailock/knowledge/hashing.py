"""Valve ids for items/abilities are MurmurHash2(lowercase class name, seed 0x31415926)."""
import struct

SEED = 0x31415926


def murmur2(key: bytes, seed: int = SEED) -> int:
    m, r = 0x5BD1E995, 24
    n = len(key)
    h = (seed ^ n) & 0xFFFFFFFF
    i = 0
    while n - i >= 4:
        k = struct.unpack_from("<I", key, i)[0]
        k = (k * m) & 0xFFFFFFFF
        k ^= k >> r
        k = (k * m) & 0xFFFFFFFF
        h = ((h * m) & 0xFFFFFFFF) ^ k
        i += 4
    rem = n - i
    if rem == 3:
        h ^= key[i + 2] << 16
    if rem >= 2:
        h ^= key[i + 1] << 8
    if rem >= 1:
        h ^= key[i]
        h = (h * m) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * m) & 0xFFFFFFFF
    h ^= h >> 15
    return h


def ability_id(class_name: str) -> int:
    return murmur2(class_name.lower().encode())
