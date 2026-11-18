![image](https://github.com/globocom/m3u8/actions/workflows/main.yml/badge.svg) [![image](https://badge.fury.io/py/m3u8.svg)](https://badge.fury.io/py/m3u8)

# m3u8

Python [m3u8](https://tools.ietf.org/html/rfc8216) parser.

# Documentation

## Loading a playlist

To load a playlist into an object from uri, file path or directly from
string, use the `load/loads` functions:

```python
import m3u8

playlist = m3u8.load('http://videoserver.com/playlist.m3u8')  # this could also be an absolute filename
print(playlist.segments)
print(playlist.target_duration)

# if you already have the content as string, use

playlist = m3u8.loads('#EXTM3U8 ... etc ... ')
```

## Dumping a playlist

To dump a playlist from an object to the console or a file, use the
`dump/dumps` functions:

``` python
import m3u8

playlist = m3u8.load('http://videoserver.com/playlist.m3u8')
print(playlist.dumps())

# if you want to write a file from its content

playlist.dump('playlist.m3u8')
```

# Supported tags

-   [\#EXT-X-TARGETDURATION](https://tools.ietf.org/html/rfc8216#section-4.3.3.1)
-   [\#EXT-X-MEDIA-SEQUENCE](https://tools.ietf.org/html/rfc8216#section-4.3.3.2)
-   [\#EXT-X-DISCONTINUITY-SEQUENCE](https://tools.ietf.org/html/rfc8216#section-4.3.3.3)
-   [\#EXT-X-PROGRAM-DATE-TIME](https://tools.ietf.org/html/rfc8216#section-4.3.2.6)
-   [\#EXT-X-MEDIA](https://tools.ietf.org/html/rfc8216#section-4.3.4.1)
-   [\#EXT-X-PLAYLIST-TYPE](https://tools.ietf.org/html/rfc8216#section-4.3.3.5)
-   [\#EXT-X-KEY](https://tools.ietf.org/html/rfc8216#section-4.3.2.4)
-   [\#EXT-X-STREAM-INF](https://tools.ietf.org/html/rfc8216#section-4.3.4.2)
-   [\#EXT-X-VERSION](https://tools.ietf.org/html/rfc8216#section-4.3.1.2)
-   [\#EXT-X-ALLOW-CACHE](https://datatracker.ietf.org/doc/html/draft-pantos-http-live-streaming-07#section-3.3.6)
-   [\#EXT-X-ENDLIST](https://tools.ietf.org/html/rfc8216#section-4.3.3.4)
-   [\#EXTINF](https://tools.ietf.org/html/rfc8216#section-4.3.2.1)
-   [\#EXT-X-I-FRAMES-ONLY](https://tools.ietf.org/html/rfc8216#section-4.3.3.6)
-   [\#EXT-X-BITRATE](https://datatracker.ietf.org/doc/html/draft-pantos-hls-rfc8216bis#section-4.4.4.8)
-   [\#EXT-X-BYTERANGE](https://tools.ietf.org/html/rfc8216#section-4.3.2.2)
-   [\#EXT-X-I-FRAME-STREAM-INF](https://tools.ietf.org/html/rfc8216#section-4.3.4.3)
-   [\#EXT-X-IMAGES-ONLY](https://github.com/image-media-playlist/spec/blob/master/image_media_playlist_v0_4.pdf)
-   [\#EXT-X-IMAGE-STREAM-INF](https://github.com/image-media-playlist/spec/blob/master/image_media_playlist_v0_4.pdf)
-   [\#EXT-X-TILES](https://github.com/image-media-playlist/spec/blob/master/image_media_playlist_v0_4.pdf)
-   [\#EXT-X-DISCONTINUITY](https://tools.ietf.org/html/rfc8216#section-4.3.2.3)
-   \#EXT-X-CUE-OUT
-   \#EXT-X-CUE-OUT-CONT
-   \#EXT-X-CUE-IN
-   \#EXT-X-CUE-SPAN
-   \#EXT-OATCLS-SCTE35
-   [\#EXT-X-INDEPENDENT-SEGMENTS](https://tools.ietf.org/html/rfc8216#section-4.3.5.1)
-   [\#EXT-X-MAP](https://tools.ietf.org/html/rfc8216#section-4.3.2.5)
-   [\#EXT-X-START](https://tools.ietf.org/html/rfc8216#section-4.3.5.2)
-   [\#EXT-X-SERVER-CONTROL](https://datatracker.ietf.org/doc/html/draft-pantos-hls-rfc8216bis#section-4.4.3.8)
-   [\#EXT-X-PART-INF](https://datatracker.ietf.org/doc/html/draft-pantos-hls-rfc8216bis#section-4.4.3.7)
-   [\#EXT-X-PART](https://datatracker.ietf.org/doc/html/draft-pantos-hls-rfc8216bis#section-4.4.4.9)
-   [\#EXT-X-RENDITION-REPORT](https://datatracker.ietf.org/doc/html/draft-pantos-hls-rfc8216bis#section-4.4.5.4)
-   [\#EXT-X-SKIP](https://datatracker.ietf.org/doc/html/draft-pantos-hls-rfc8216bis#section-4.4.5.2)
-   [\#EXT-X-SESSION-DATA](https://tools.ietf.org/html/rfc8216#section-4.3.4.4)
-   [\#EXT-X-PRELOAD-HINT](https://datatracker.ietf.org/doc/html/draft-pantos-hls-rfc8216bis-09#section-4.4.5.3)
-   [\#EXT-X-SESSION-KEY](https://tools.ietf.org/html/rfc8216#section-4.3.4.5)
-   [\#EXT-X-DATERANGE](https://tools.ietf.org/html/rfc8216#section-4.3.2.7)
-   [\#EXT-X-GAP](https://tools.ietf.org/html/draft-pantos-hls-rfc8216bis-05#section-4.4.2.7)
-   [\#EXT-X-CONTENT-STEERING](https://tools.ietf.org/html/draft-pantos-hls-rfc8216bis-10#section-4.4.6.64)

# Aligning positions across playlists

The same live program can be published as several media playlists (for
example a video and an audio rendition) whose segments have different
durations, are cut asynchronously and slide independently. `align_segment`
finds the same program position in another playlist using
`EXT-X-PROGRAM-DATE-TIME` anchors and `EXT-X-DISCONTINUITY` boundaries,
extrapolating the program clock across segments that do not carry their own
PDT:

```python
import m3u8
from m3u8 import align_segment

source = m3u8.load("http://example.com/video.m3u8")
target = m3u8.load("http://example.com/audio.m3u8")

# 4.5s into the second source segment
result = align_segment(source, 1, target, offset=4.5)
if result.status == "ok":
    target_segment = result.target_segment      # m3u8.Segment
    target_offset = result.offset               # seconds inside it
    program_time = result.program_date_time     # absolute program clock
    worst_case_error = result.error             # estimated error, seconds
elif result.status == "gap":
    ...  # position found, but the target segment carries EXT-X-GAP
else:
    ...  # result.reason explains why the position cannot be mapped
```

The source segment may be given as a zero based index or a `Segment` object
(including one kept across a playlist reload, matched by media sequence).
The result `status` is `ok`, `gap` or `error`, with a machine readable
`reason` on failure:

-   `no_common_anchor` — the playlists do not share a PDT anchor covering
    the position (including a target window without any PDT),
-   `clock_backwards` — a PDT tag moved backwards without a discontinuity,
-   `cross_discontinuity` — the position falls into a discontinuity gap or
    needs an unanchored segment on the other side of a discontinuity,
-   `outside_window` — the position is older or newer than every segment of
    the target sliding window,
-   `offset_out_of_bounds` — the offset is not within the source segment,
-   `segment_not_in_playlist` / `empty_playlist` — invalid input.

`error` is a worst-case estimate in seconds: explicit PDT anchors are exact,
while extrapolated positions accumulate the playlist target duration once
per segment away from the nearest anchor, plus any clock drift measured
between consecutive anchors. The per-segment assumption can be overridden
with `extrapolation_bound`.

# Frequently Asked Questions

-   [FAQ](https://github.com/globocom/m3u8/wiki/FAQ)

# Running Tests

``` bash
$ ./runtests
```

# Contributing

All contributions are welcome, but we will merge a pull request if, and
only if, it

- Has tests
- Follows the code conventions

If you plan to implement a new feature or something that will take more
than a few minutes, please open an issue to make sure we don't work on
the same thing.
