# NeoX animation clips: `.cpdanimation` (`RAWANIMA`)

This describes the binary animation format found in Identity V (`doc-res-chr_*`
archives), how it was worked out, and what is still unknown. The decoder is
`core/animation/cpdanimation.py`; the investigation tool is
`tools/probe_cpdanimation.py`.

Fields marked **[verified]** were checked over all 5993 `.cpdanimation` files of
`doc-res-chr_boss` (parse exactly to the end of the chunk) and, where possible,
against ground truth (the skeleton's reference pose, see §6).

---

## 1. The three animation assets

| extension | content | role |
|---|---|---|
| `.skeleton` | JSON | joint names, parent indices, reference (bind) pose |
| `.animation` | JSON | wrapper: event/string tracks; lists its `.cpdanimation` in `Dependices` |
| `.cpdanimation` | binary, magic `RAWANIMA` | the actual keyframes |

`.animation` and `.skeleton` share a JSON header:
`{"_FileHeader": {"ResourceVersion": "2021.1", "AssetType": "...", ...}}`.

### Skeleton reference pose **[verified]**

`reference_pose` is base64 of a `TypedIndexArray<SkeletonJointIndex,SimdTransformf>`:
12 × float32 per joint, **local** to the parent:

```
translation x, y, z, 1 | rotation quaternion x, y, z, w | scale x, y, z, 1
```

## 2. File header **[verified]**

```
0x00  char[8]  "RAWANIMA"
0x08  u32      file size (always equals the real size)
0x0C  u32      0                      (unknown)
0x10  u32      0, or 1 in 4 files     (unknown)
0x14  u32      offset of the first chunk
0x18  u8[16]   id / hash              (unknown)
0x28  u32      length, then that many bytes: skeleton path, e.g. "../boy_axe_caodi.skeleton"
```

The skeleton path's file name matches the `"name"` field inside the `.skeleton`
JSON; that is how animations are paired with skeletons.

## 3. Chunks **[verified]**

From the first-chunk offset to the end of the file: `char[4] tag, u32 size,
size bytes`. Every file is `HEAD DATA NAME` (one file also has `META`), and the
chunks end exactly at the end of the file.

### HEAD (32 bytes)

| offset | type | meaning | status |
|---|---|---|---|
| 0 | f32 | frames per second (30 or 60) | **[verified]** |
| 4 | f32 | duration in seconds; `duration * fps` = last key frame | **[verified]** |
| 8 | u8 | looping flag (names with "loop": 112×1 vs 9×0; names ending "_end"/"_start": 9×1 vs 297×0) | likely |
| 9..14 | | `01 01 00 02 00 00` in every file | unknown |
| 15..17 | u8[3] | key encoding: `01 02 03` (92.2 %), `01 01 03` (7.5 %), `01 01 02`, `01 00 00` | see §5 |
| 18..31 | | zero | |

The float `3.3333` (`55 55 55 40`) is a common duration, which is why
`core/npk/detection.py` historically found the bytes `00 00 00 00 00 55 55` in
animation files.

### NAME **[verified]**

`u32 count`, then `count` × (`u32 length`, UTF-8 bytes), zero-padded to 16 bytes.
Entry 0 is the clip name; entries 1..n are the animated joints, in the same order
as the tracks in DATA. Joint names match `.skeleton` `joint_names`, but the order
and subset differ, so match by name.

## 4. DATA for encoding `01 02 03` **[verified]**

```
u8 0x04                         (always 4; meaning unknown)
for each joint in NAME[1:]:
    translation track
    scale track
    rotation track
zero padding to a 16-byte boundary
```

A track is `u16 key_count` followed by `key_count` × (`u16 frame`, payload):

| track | payload |
|---|---|
| translation | 3 × float16 (x, y, z) |
| scale | 3 × float16 (x, y, z) |
| rotation | u32 packed quaternion (§4.1) |

Keys are sparse: only frames where the value changes are stored, frames strictly
increase, and a bone that never moves has one key at frame 0. Empty tracks have
`key_count = 0`.

**Quirk:** trailing zero bytes are trimmed before padding, so the count of the
very last (empty) track can be cut off. A count that lies past the end of the
chunk reads as 0.

### 4.1 Rotation: 32-bit "smallest three" **[verified]**

```
bits 30-31  index i of the dropped (largest-magnitude) component
bits 20-29  component (i+1) % 4
bits 10-19  component (i+2) % 4
bits  0- 9  component (i+3) % 4
each 10-bit value v -> (v / 1023 * 2 - 1) / sqrt(2)
dropped component = sqrt(1 - a^2 - b^2 - c^2)   (always positive)
```

Components are in `(x, y, z, w)` order, like the skeleton. Consecutive keys can
flip sign (`q` and `-q` are the same rotation); an exporter should flip each key
onto the same hemisphere as the previous one before interpolating.

## 5. Other encodings: not decoded

`01 01 03` (449 files), `01 01 02` (14) and `01 00 00` (4) are rejected with
`UnsupportedEncodingError`. Their DATA does not start with `0x04`, and they are
much larger per key, which suggests the translation (and in `01 00 00` all
channels) use a different, probably wider, encoding. Comparing the HEAD encoding
bytes, a reasonable guess is one code per channel (translation, scale, rotation),
so `01 01 03` would differ from `01 02 03` only in the scale channel. **Unverified.**

## 6. How this was worked out (and how to check changes)

1. **Text first.** `.skeleton` and `.animation` turned out to be JSON; the
   `.animation` file names its `.cpdanimation`, so the binary file holds the keys.
2. **Smallest file first.** A 173-byte file shows the whole structure: magic, a
   size field equal to the file size, a first-chunk offset, then `HEAD`, `DATA`,
   `NAME` chunks.
3. **Survey everything.** Walking the chunks of all 5993 files confirmed the
   container and showed that HEAD floats are fps and duration.
4. **Patterns in DATA.** Repeating 6-byte records `01 00 xx xx xx xx`,
   `02 00 …` are `u16 frame` + 4-byte payload; half floats such as `00 3c ff 3b 00 3c`
   (1, 1, 1) are scale.
5. **Let the data reject hypotheses.** Each guess was run over every file and
   kept only if it parsed exactly to the end of the chunk with zero padding left.
   This found the empty-track truncation quirk, and that the track order is
   translation, scale, rotation rather than rotation, translation, scale: in the
   wrong grouping, translations came out correct but one joint off.
6. **Smoothness for the quaternion.** Real motion changes little between
   consecutive frames, so candidate packings were scored by the median
   `|dot(q[t], q[t+1])|`: smallest-three with the index in the top bits scores
   0.9996; every other candidate scored ≤ 0.98.
7. **Ground truth for the details.** A bone the animation does not move has a
   single key, which should equal the skeleton reference pose. That settled the
   component order (cyclic after the dropped index) and the axis order (xyzw).

Run `python tools/probe_cpdanimation.py verify <folder>` after any decoder change.
On `doc-res-chr_boss` (3668 animations matched to a skeleton) the current decoder
gives:

| channel | exact match to reference pose | clearly different pose |
|---|---|---|
| rotation (`1-|dot| < 1e-5`) | 64.5 % | 19.6 % (`> 0.01`) |
| translation (relative `< 0.1 %`) | 81.5 % | 10.3 % (`> 10 %`) |
| scale (relative `< 0.1 %`) | 89.3 % | 6.8 % (`> 10 %`) |

A wrong decoder produces small scattered errors everywhere instead of this
two-peaked distribution.

## 7. Open questions

1. Encodings `01 01 03`, `01 01 02`, `01 00 00` (§5).
2. The leading `0x04` byte of DATA, HEAD bytes 9..14, header fields `0x0C`, `0x10`
   and the 16-byte id.
3. The `META` chunk (one file).
4. About 10 % of constant rotations differ from the reference pose by
   `1e-3`…`1e-2`. These may be genuinely different poses, or a sign that the
   dequantisation constant is slightly off; check against an in-game capture.
5. Root motion: whether the clip's root joint carries world movement, and in
   which space.
6. The `.animation` JSON event tracks (`StringTrack`), which likely hold
   sounds/effects timed to the clip.

## 8. Exporting (not implemented)

Everything needed for an export is decoded: per joint, sparse keys of local
translation, rotation (xyzw) and scale, plus the skeleton hierarchy and bind
pose. A glTF exporter would write one animation with a sampler per channel
(`input` = `frame / fps`, `output` = values, `interpolation` = `LINEAR`) targeting
the joint nodes. Joints without a translation or scale track keep the skeleton's
reference value. Rotations must be hemisphere-aligned first (§4.1).
