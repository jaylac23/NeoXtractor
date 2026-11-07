#!/usr/bin/env python3
"""Inspect, survey and verify NeoX ``.cpdanimation`` (RAWANIMA) files.

The format is described in ``docs/neox_cpdanimation.md``. Use this tool while
working on the parser in ``core/animation/cpdanimation.py``:

    # one file: header, chunks, per-joint key counts, sample keys
    python tools/probe_cpdanimation.py info  path/to/file.cpdanimation

    # a folder of extracted files: how many decode, grouped by key encoding
    python tools/probe_cpdanimation.py survey path/to/extracted_folder

    # a folder that also contains the .skeleton files: check decoded keys
    # against each skeleton's reference pose (the ground truth)
    python tools/probe_cpdanimation.py verify path/to/extracted_folder

``verify`` relies on a simple fact: a bone that an animation does not move has a
single key, and that key should equal the skeleton's reference pose. A correct
decoder therefore matches the reference *exactly* for most such bones, while
the rest differ by a large amount (the animation really holds a different
pose). A wrong decoder produces small, scattered errors everywhere instead.
"""

from __future__ import annotations

import argparse
import collections
import glob
import os
import struct
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.animation import (  # noqa: E402  pylint: disable=wrong-import-position
    UnsupportedEncodingError,
    parse_cpdanimation,
    parse_skeleton,
)


def _read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def cmd_info(args):
    data = _read(args.file)
    try:
        anim = parse_cpdanimation(data)
        error = None
    except UnsupportedEncodingError as e:
        anim, error = None, e
    print(f"file          {args.file} ({len(data)} bytes)")
    print(f"header        size field={struct.unpack_from('<I', data, 8)[0]}"
          f" unk0C={struct.unpack_from('<I', data, 0x0C)[0]}"
          f" unk10={struct.unpack_from('<I', data, 0x10)[0]}"
          f" first_chunk=0x{struct.unpack_from('<I', data, 0x14)[0]:x}")
    print(f"id            {data[0x18:0x28].hex(' ')}")
    if error:
        print(f"!! {error}")
        return
    print(f"name          {anim.name}")
    print(f"skeleton      {anim.skeleton_path}")
    print(f"fps/duration  {anim.fps:g} fps, {anim.duration:.4f} s -> {anim.frame_count} frames,"
          f" looping={anim.looping}")
    print(f"HEAD          {anim.chunks['HEAD'].hex(' ')}")
    print(f"chunks        {', '.join(f'{k}({len(v)})' for k, v in anim.chunks.items())}")
    print(f"joints        {len(anim.joints)}")
    for joint in anim.joints[: args.limit]:
        print(f"  {joint.name:32s} T={len(joint.translations):4d} S={len(joint.scales):4d}"
              f" R={len(joint.rotations):4d}")
        if args.keys:
            for label, keys in (("T", joint.translations), ("S", joint.scales),
                                ("R", joint.rotations)):
                for frame, value in keys[: args.keys]:
                    print(f"      {label} frame {frame:4d}: "
                          + ", ".join(f"{v:+.4f}" for v in value))
    if len(anim.joints) > args.limit:
        print(f"  ... {len(anim.joints) - args.limit} more (use --limit)")


def cmd_survey(args):
    stats = collections.Counter()
    examples = {}
    for path in glob.glob(os.path.join(args.folder, "**", "*.cpdanimation"), recursive=True):
        data = _read(path)
        encoding = b""
        try:
            anim = parse_cpdanimation(data)
            encoding, ok = anim.encoding, "ok"
        except UnsupportedEncodingError as e:
            ok = "unsupported"
            encoding = str(e).split("encoding ")[-1].split(" is")[0].encode()
        except Exception as e:  # pylint: disable=broad-exception-caught
            ok = f"error: {e}"
        key = (encoding.hex(" ") if isinstance(encoding, bytes) and ok == "ok"
               else encoding.decode(errors="replace"), ok)
        stats[key] += 1
        examples.setdefault(key, path)
    total = sum(stats.values())
    print(f"{total} files")
    for (encoding, ok), count in stats.most_common():
        print(f"  {count:6d} ({count / total:6.1%})  encoding={encoding:9s} {ok}")
        if ok != "ok":
            print(f"           e.g. {examples[(encoding, ok)]}")


def _load_skeletons(folder):
    skeletons = {}
    for path in glob.glob(os.path.join(folder, "**", "*.skeleton"), recursive=True):
        try:
            skel = parse_skeleton(_read(path))
        except Exception:  # pylint: disable=broad-exception-caught
            continue
        skeletons[skel.name] = skel
    return skeletons


def cmd_verify(args):
    skeletons = _load_skeletons(args.folder)
    print(f"{len(skeletons)} skeletons loaded")
    rot_err, pos_err, scl_err = [], [], []
    matched = 0
    for path in glob.glob(os.path.join(args.folder, "**", "*.cpdanimation"), recursive=True):
        try:
            anim = parse_cpdanimation(_read(path))
        except Exception:  # pylint: disable=broad-exception-caught
            continue
        key = os.path.splitext(os.path.basename(anim.skeleton_path.replace("\\", "/")))[0]
        skel = skeletons.get(key)
        if skel is None:
            continue
        matched += 1
        for joint in anim.joints:
            i = skel.index(joint.name)
            if i < 0:
                continue
            if len(joint.rotations) == 1:
                q = np.array(joint.rotations[0][1])
                rot_err.append(1.0 - abs(float(np.dot(q, skel.rotations[i]))))
            for keys, ref, out in ((joint.translations, skel.translations[i], pos_err),
                                   (joint.scales, skel.scales[i], scl_err)):
                if len(keys) == 1:
                    v = np.array(keys[0][1])
                    out.append(float(np.abs(v - ref).max() / max(1e-3, np.abs(ref).max())))
        if args.max and matched >= args.max:
            break
    print(f"{matched} animations matched to a skeleton\n")

    def report(label, errors, buckets):
        errors = np.array(errors)
        print(f"{label}: {len(errors)} constant keys")
        if len(errors) == 0:
            return
        edges = [0.0, *buckets, np.inf]
        for lo, hi in zip(edges, edges[1:]):
            share = ((errors >= lo) & (errors < hi)).mean()
            print(f"    [{lo:<8g}, {hi:<8g}) {share:6.1%} {'#' * int(share * 50)}")

    report("rotation   (1 - |dot|)", rot_err, [1e-5, 1e-4, 1e-3, 1e-2])
    report("translation (relative)", pos_err, [1e-3, 1e-2, 1e-1])
    report("scale       (relative)", scl_err, [1e-3, 1e-2, 1e-1])
    print("\nA correct decoder puts most keys in the first bucket and the rest in the last"
          " one (those animations really hold a different pose).")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("info", help="describe one .cpdanimation file")
    p.add_argument("file")
    p.add_argument("--limit", type=int, default=20, help="joints to list")
    p.add_argument("--keys", type=int, default=0, help="keys to print per track")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("survey", help="decode every .cpdanimation in a folder")
    p.add_argument("folder")
    p.set_defaults(func=cmd_survey)

    p = sub.add_parser("verify", help="compare decoded keys with skeleton reference poses")
    p.add_argument("folder")
    p.add_argument("--max", type=int, default=0, help="stop after N matched animations")
    p.set_defaults(func=cmd_verify)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
