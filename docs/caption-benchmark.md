# Dynamic caption benchmark

Measured on Apple M3 Pro with the same 48.234-second portrait reel across all runs. Source cuts, fit, zooms, encoder and 1.1× playback speed were held constant. The original checksum was unchanged.

| Caption style | ASS events | Wall time | MP4 size | Delta vs static |
|---|---:|---:|---:|---:|
| Static control | 24 | 63.16 s | 15.05 MB | — |
| Mystical Gold | 42 | 69.24 s | 15.25 MB | +9.6% |
| High Impact | 214 | 66.01 s | 15.25 MB | +4.5% |
| Cinematic Minimal | 37 | 63.17 s | 15.23 MB | +0.0% |
| Educational | 211 | 64.75 s | 14.97 MB | +2.5% |

One full render per preset. Worker caption generation took about 0.75–2.14 ms. The HTML preview is approximate and does not run an encoder. ASS events follow words and lines rather than output frames. A real FFmpeg glyph test covers Hindi/English shaping; the pilot footage itself uses English.
