"""Reader for NeoX JSON ``.skeleton`` files (``"AssetType" : "Skeleton"``)."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass

import numpy as np


@dataclass
class Skeleton:
    """Joint hierarchy and bind (reference) pose of a NeoX skeleton."""

    name: str
    joint_names: list[str]
    parents: list[int]
    # Local transforms per joint, from the base64 SimdTransformf array:
    translations: np.ndarray  # (n, 3)
    rotations: np.ndarray  # (n, 4) quaternions, (x, y, z, w)
    scales: np.ndarray  # (n, 3)

    def index(self, joint_name: str) -> int:
        """Index of a joint by name, or -1."""
        try:
            return self.joint_names.index(joint_name)
        except ValueError:
            return -1


def parse_skeleton(data: bytes) -> Skeleton:
    """Parse a JSON skeleton file."""
    doc = json.loads(data.decode("utf-8"))
    names = list(doc["joint_names"])
    # SimdTransformf = 12 floats: translation (x, y, z, 1), rotation (x, y, z, w),
    # scale (x, y, z, 1).
    pose = np.frombuffer(base64.b64decode(doc.get("reference_pose") or ""), "<f4")
    pose = pose.reshape(len(names), 12) if pose.size else np.zeros((len(names), 12))
    return Skeleton(
        name=doc.get("name", ""),
        joint_names=names,
        parents=list(doc["joint_parent_indices"]),
        translations=pose[:, 0:3].astype(np.float64),
        rotations=pose[:, 4:8].astype(np.float64),
        scales=pose[:, 8:11].astype(np.float64),
    )
