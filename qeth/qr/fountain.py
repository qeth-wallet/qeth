"""BC-UR fountain code (rateless part generation), BCR-2024-001.

For ``seqNum > seqLen`` a part carries a pseudo-random XOR *mix* of fragments,
chosen so the receiver reconstructs the message from ANY ~seqLen parts — no
waiting for a specific fragment, which is what removes the animated-QR's
coupon-collector tail. The device rebuilds the exact same mix from
``seqNum + checksum``, so this MUST be bit-identical to the reference; it's
pinned to the spec's Wolf test vector in tests/test_qr_fountain.py.
"""

from __future__ import annotations

import hashlib

_MASK64 = (1 << 64) - 1


def _rotl(x: int, k: int) -> int:
    return ((x << k) | (x >> (64 - k))) & _MASK64


class Xoshiro256:
    """Xoshiro256** seeded from a 32-byte value (its four uint64 state words are
    the seed's big-endian 8-byte groups)."""

    def __init__(self, seed: bytes) -> None:
        if len(seed) != 32:
            raise ValueError("Xoshiro256 seed must be 32 bytes")
        self._s = [int.from_bytes(seed[i * 8:(i + 1) * 8], "big") for i in range(4)]

    def next(self) -> int:
        s = self._s
        result = (_rotl((s[1] * 5) & _MASK64, 7) * 9) & _MASK64
        t = (s[1] << 17) & _MASK64
        s[2] ^= s[0]
        s[3] ^= s[1]
        s[1] ^= s[2]
        s[0] ^= s[3]
        s[2] ^= t
        s[3] = _rotl(s[3], 45)
        return result

    def next_double(self) -> float:
        return self.next() / (1 << 64)          # [0.0, 1.0)

    def next_int(self, low: int, high: int) -> int:
        return int(self.next_double() * (high - low)) + low   # [low, high)


class _RandomSampler:
    """Walker-Vose alias sampler over non-negative weights."""

    def __init__(self, weights: list[float]) -> None:
        n = len(weights)
        total = sum(weights)
        scaled = [w * n / total for w in weights]
        self._prob = [0.0] * n
        self._alias = [0] * n
        small = [i for i in range(n) if scaled[i] < 1.0]
        large = [i for i in range(n) if scaled[i] >= 1.0]
        while small and large:
            s = small.pop()
            g = large.pop()
            self._prob[s] = scaled[s]
            self._alias[s] = g
            scaled[g] = (scaled[g] + scaled[s]) - 1.0
            (small if scaled[g] < 1.0 else large).append(g)
        for i in large:
            self._prob[i] = 1.0
        for i in small:
            self._prob[i] = 1.0

    def next(self, r1: float, r2: float) -> int:
        i = int(len(self._prob) * r1)
        return i if r2 < self._prob[i] else self._alias[i]


def _choose_degree(seq_len: int, rng: Xoshiro256) -> int:
    # Degree probabilities are the harmonic series 1/1, 1/2, …, 1/seqLen.
    sampler = _RandomSampler([1.0 / (i + 1) for i in range(seq_len)])
    return sampler.next(rng.next_double(), rng.next_double()) + 1


def choose_fragments(seq_num: int, seq_len: int, checksum: int) -> set[int]:
    """The fragment indexes part ``seq_num`` carries. ``seqNum`` in 1..seqLen is
    the single pure fragment ``seqNum-1``; beyond that, a pseudo-random set of
    ``degree`` fragments (seeded by ``SHA256(seqNum‖checksum)``)."""
    if seq_num <= seq_len:
        return {seq_num - 1}
    seed = hashlib.sha256(
        seq_num.to_bytes(4, "big") + checksum.to_bytes(4, "big")).digest()
    rng = Xoshiro256(seed)
    degree = _choose_degree(seq_len, rng)
    remaining = list(range(seq_len))
    chosen: list[int] = []
    while len(chosen) < degree:
        chosen.append(remaining.pop(rng.next_int(0, len(remaining))))
    return set(chosen)


def mix(fragments: list[bytes], indexes: set[int]) -> bytes:
    """XOR the chosen fragments together (all equal length)."""
    out = bytearray(len(fragments[0]))
    for i in indexes:
        fragment = fragments[i]
        for j in range(len(out)):
            out[j] ^= fragment[j]
    return bytes(out)
