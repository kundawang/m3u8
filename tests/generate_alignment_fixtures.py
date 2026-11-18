#!/usr/bin/env python3
# Copyright 2014 Globo.com Player authors. All rights reserved.
# Use of this source code is governed by a MIT License
# license that can be found in the LICENSE file.
"""
Generate deterministic fixtures for the cross-manifest alignment tests.

It models one live program published as two media playlists whose segments
are cut asynchronously:

* the video rendition uses 10s segments,
* the audio rendition uses 6s segments,

both starting at the same program clock but with independent media sequence
numbering. A discontinuity happens at the same program position on both
playlists (with a 5s wall-clock jump), only some segments carry an explicit
EXT-X-PROGRAM-DATE-TIME tag and one video segment is missing (EXT-X-GAP).

For each rendition the full playlist and several sliding windows are written.
The windows deliberately cover different program-time ranges so that tests
can exercise overlapping and non-overlapping windows.

Run with:  python3 tests/generate_alignment_fixtures.py
"""

import os
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(HERE, "playlists", "alignment")

EPOCH = datetime(
    2026,
    1,
    1,
    0,
    0,
    0,
    tzinfo=timezone.utc,  # noqa: UP017 - datetime.UTC needs 3.11
)

# program-time layout, in seconds since EPOCH
VIDEO_BOUNDS = [0, 10, 25, 35, 45, 55, 65, 75, 85]
AUDIO_BOUNDS = [0, 6, 12, 25, 31, 37, 43, 49, 55, 61, 67, 73, 79, 85]
DISCONTINUITY_AT = 10.0
WALL_CLOCK_JUMP = 5.0
GAP_VIDEO_INDEX = 6  # program time 65-75s is unavailable

# (name, inclusive index range over the full rendition segment list)
VIDEO_WINDOWS = {
    "full": (0, 7),
    "early": (0, 4),  # contains the discontinuity and the second region start
    "late": (2, 7),  # second region only, sliding window moved on
    "gap": (4, 7),  # window containing the GAP segment
}
AUDIO_WINDOWS = {
    "full": (0, 12),
    "early": (0, 7),
    "late": (4, 12),
    "mid": (8, 12),
}


def wall_clock(program_second):
    """Map a program-time second onto the advertised wall clock."""
    if program_second >= DISCONTINUITY_AT:
        program_second += WALL_CLOCK_JUMP
    return EPOCH + timedelta(seconds=program_second)


def pdt_tag(program_second):
    return f"#EXT-X-PROGRAM-DATE-TIME:{wall_clock(program_second).isoformat()}"


def build_rendition(kind, bounds, explicit_pdt_indices, gap_index=None):
    """Return the body lines describing one rendition's segments."""
    lines = []
    for index in range(len(bounds) - 1):
        start, end = bounds[index], bounds[index + 1]
        duration = end - start
        if start == DISCONTINUITY_AT:
            lines.append("#EXT-X-DISCONTINUITY")
        if index in explicit_pdt_indices:
            lines.append(pdt_tag(start))
        if gap_index is not None and index == gap_index:
            lines.append("#EXT-X-GAP")
        lines.append(f"#EXTINF:{duration:d},")
        lines.append(f"{kind}_{index:02d}.ts")
    return lines


def write_playlist(
    filename, target_duration, media_sequence, body, endlist=False, leading_pdt=None
):
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:6",
        f"#EXT-X-TARGETDURATION:{target_duration:d}",
        f"#EXT-X-MEDIA-SEQUENCE:{media_sequence:d}",
    ]
    if leading_pdt is not None and not (
        body and body[0].startswith("#EXT-X-PROGRAM-DATE-TIME")
    ):
        lines.append(pdt_tag(leading_pdt))
    lines.extend(body)
    if endlist:
        lines.append("#EXT-X-ENDLIST")
    path = os.path.join(OUTPUT_DIR, filename)
    with open(path, "w", encoding="utf-8") as fileobj:
        fileobj.write("\n".join(lines) + "\n")


def slice_body(full_body, first, last):
    """
    Slice a segment body list, keeping every tag attached to a segment.

    EXT-X-DISCONTINUITY, EXT-X-PROGRAM-DATE-TIME and EXT-X-GAP precede the
    segment they describe, so they are accumulated into the following
    segment block.
    """
    blocks = []
    current = []
    for line in full_body:
        current.append(line)
        if line.endswith(".ts"):
            blocks.append(current)
            current = []
    sliced = []
    for block in blocks[first : last + 1]:
        sliced.extend(block)
    return sliced


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # video: PDT only on the first segment and on the first post-discontinuity
    # segment; every later clock must be extrapolated from EXTINF durations.
    video_body = build_rendition(
        "video", VIDEO_BOUNDS, explicit_pdt_indices={0, 1}, gap_index=GAP_VIDEO_INDEX
    )
    # audio: PDT on first segment, on the discontinuity boundary and on one
    # mid-window segment, exercising both extrapolation and a fresh anchor.
    audio_body = build_rendition("audio", AUDIO_BOUNDS, explicit_pdt_indices={0, 2, 5})

    for name, (first, last) in VIDEO_WINDOWS.items():
        # Refreshed windows re-anchor their clock on the first segment so the
        # GAP tag remains reachable even though the later segments have no PDT.
        write_playlist(
            f"video_{name}.m3u8",
            target_duration=10,
            media_sequence=900 + first,
            body=slice_body(video_body, first, last),
            leading_pdt=VIDEO_BOUNDS[first],
        )
    for name, (first, last) in AUDIO_WINDOWS.items():
        # The "mid" window has no PDT tag at all to model a playlist whose
        # program clock cannot be recovered; every other window is anchored.
        leading_pdt = None if name == "mid" else AUDIO_BOUNDS[first]
        write_playlist(
            f"audio_{name}.m3u8",
            target_duration=6,
            media_sequence=4000 + first,
            body=slice_body(audio_body, first, last),
            leading_pdt=leading_pdt,
        )

    print(f"alignment fixtures written to {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
