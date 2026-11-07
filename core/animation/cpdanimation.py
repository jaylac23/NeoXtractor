"""Parser for NeoX ``.cpdanimation`` files (magic ``RAWANIMA``).

See ``docs/neox_cpdanimation.md`` for the format description, how each field
was worked out, and which parts are still unknown.

This module only depends on the standard library so it can be used from tools
without importing the Qt application.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field

MAGIC = b"RAWANIMA"

# HEAD bytes 15..17 describe the key encodings. Only this combination is decoded:
# translation = 3 x float16, scale = 3 x float16, rotation = 32-bit "smallest three".
SUPPORTED_ENCODING = b"\x01\x02\x03"

_SQRT1_2 = 1.0 / math.sqrt(2.0)


class UnsupportedEncodingError(NotImplementedError):
    """Raised for key encodings that have not been reverse engineered yet."""


@dataclass
class JointTrack:
    """Keyframes of one joint. Frames are integers; time = frame / fps."""

    name: str
    translations: list[tuple[int, tuple[float, float, float]]] = field(default_factory=list)
    scales: list[tuple[int, tuple[float, float, float]]] = field(default_factory=list)
    # Quaternions in (x, y, z, w) order, matching the skeleton reference pose.
    rotations: list[tuple[int, tuple[float, float, float, float]]] = field(default_factory=list)


@dataclass
class CpdAnimation:
    """A decoded RAWANIMA animation clip."""

    name: str
    skeleton_path: str
    fps: float
    duration: float
    looping: bool
    encoding: bytes
    joints: list[JointTrack]
    chunks: dict[str, bytes]

    @property
    def frame_count(self) -> int:
        """Number of frames, including frame 0."""
        return round(self.duration * self.fps) + 1


def decode_quaternion(value: int) -> tuple[float, float, float, float]:
    """Decode a 32-bit "smallest three" quaternion into (x, y, z, w).

    Bits 30-31 hold the index of the dropped (largest) component. Bits 20-29,
    10-19 and 0-9 hold the remaining three components, in cyclic order starting
    after the dropped one, each quantised to 10 bits over [-1/sqrt2, 1/sqrt2].
    """
    dropped = value >> 30
    comps = [
        ((value >> shift) & 0x3FF) / 1023.0 * 2.0 - 1.0
        for shift in (20, 10, 0)
    ]
    comps = [c * _SQRT1_2 for c in comps]
    largest = math.sqrt(max(0.0, 1.0 - sum(c * c for c in comps)))
    quat = [0.0, 0.0, 0.0, 0.0]
    quat[dropped] = largest
    for offset, comp in enumerate(comps, start=1):
        quat[(dropped + offset) % 4] = comp
    return (quat[0], quat[1], quat[2], quat[3])


def _read_chunks(data: bytes) -> dict[str, bytes]:
    offset = struct.unpack_from("<I", data, 0x14)[0]
    chunks: dict[str, bytes] = {}
    while offset + 8 <= len(data):
        tag = data[offset : offset + 4].decode("ascii", "replace")
        size = struct.unpack_from("<I", data, offset + 4)[0]
        chunks[tag] = data[offset + 8 : offset + 8 + size]
        offset += 8 + size
    return chunks


def _read_names(payload: bytes) -> list[str]:
    count = struct.unpack_from("<I", payload, 0)[0]
    offset = 4
    result = []
    for _ in range(count):
        length = struct.unpack_from("<I", payload, offset)[0]
        result.append(payload[offset + 4 : offset + 4 + length].decode("utf-8", "replace"))
        offset += 4 + length
    return result


class _TrackReader:
    """Reads ``u16 count`` + ``count x (u16 frame, payload)`` tracks.

    The encoder drops trailing zero bytes before padding the chunk, so a count
    that lies (partly) past the end of the data reads as zero.
    """

    def __init__(self, data: bytes, offset: int):
        self.data = data
        self.offset = offset

    def read(self, payload_size: int) -> list[tuple[int, bytes]]:
        data = self.data
        count = (
            struct.unpack_from("<H", data + b"\0\0", self.offset)[0]
            if self.offset < len(data)
            else 0
        )
        self.offset += 2
        if count == 0:
            return []
        step = 2 + payload_size
        end = self.offset + count * step
        if end > len(data):
            raise ValueError(f"track at {self.offset - 2} runs past the DATA chunk")
        keys = [
            (
                struct.unpack_from("<H", data, self.offset + i * step)[0],
                data[self.offset + i * step + 2 : self.offset + (i + 1) * step],
            )
            for i in range(count)
        ]
        if any(a >= b for (a, _), (b, _) in zip(keys, keys[1:])):
            raise ValueError(f"track at {self.offset - 2} has non-increasing frames")
        self.offset = end
        return keys


def _half3(payload: bytes) -> tuple[float, float, float]:
    x, y, z = struct.unpack("<3e", payload)
    return (x, y, z)


def parse_cpdanimation(data: bytes) -> CpdAnimation:
    """Parse a ``.cpdanimation`` file."""
    if data[:8] != MAGIC:
        raise ValueError("not a RAWANIMA file")

    path_len = struct.unpack_from("<I", data, 0x28)[0]
    skeleton_path = data[0x2C : 0x2C + path_len].decode("utf-8", "replace")

    chunks = _read_chunks(data)
    head = chunks["HEAD"]
    fps, duration = struct.unpack_from("<ff", head, 0)
    encoding = head[15:18]
    names = _read_names(chunks["NAME"])

    anim = CpdAnimation(
        name=names[0] if names else "",
        skeleton_path=skeleton_path,
        fps=fps,
        duration=duration,
        looping=bool(head[8]),
        encoding=encoding,
        joints=[],
        chunks=chunks,
    )

    if encoding != SUPPORTED_ENCODING:
        raise UnsupportedEncodingError(
            f"key encoding {encoding.hex(' ')} is not decoded yet"
        )

    reader = _TrackReader(chunks["DATA"], 1)  # byte 0 is always 0x04 (meaning unknown)
    for joint_name in names[1:]:
        track = JointTrack(joint_name)
        track.translations = [(f, _half3(p)) for f, p in reader.read(6)]
        track.scales = [(f, _half3(p)) for f, p in reader.read(6)]
        track.rotations = [
            (f, decode_quaternion(struct.unpack("<I", p)[0])) for f, p in reader.read(4)
        ]
        anim.joints.append(track)

    if any(chunks["DATA"][reader.offset :]):
        raise ValueError("unexpected non-zero bytes after the last track")
    return anim
