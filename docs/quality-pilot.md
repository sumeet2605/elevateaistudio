# Coaching reel quality pilot

Use an original, uncaptioned Elevate with Sumeet recording. A clip with existing text in its image can support a performance comparison. Use a clean source to score caption readability and placement.

## Run the evaluation

1. Record source duration, resolution, frame rate, and SHA-256. Upload a copy through the studio and check that its registered hash matches.
2. Time transcription, silence analysis, director generation, and rendering separately. Measure process wall time and, where useful, record queued wait separately. Record observed API usage or the mock label. Use Mystical Sumeet for the initial pilot.
3. Start a manual correction timer only while reviewing cuts, marking words or emphasis, adjusting framing or caption settings, and inspecting candidate previews. Pause it while the worker runs. Record each review action and the reason. Keep the original transcript revision and distinguish transcript corrections from render adjustments.
4. Listen from roughly one second before through one second after every approved seam. Check for clipped syllables, discontinuities, misplaced gaps, and lost context. A clean seam must still support the intended pacing.
5. Inspect the final playback at a phone-sized viewport with sound. Check the opening, every accepted seam, and the lower-screen safe area. Review every caption against the spoken audio. Inspect Devanagari only if the source contains it.
6. Approve the exact preview, download the final MP4, and verify output codecs, stream timing and original SHA-256. Reopen the export in Chrome and check seeking.
7. Save the scorecard and measurements with the source hash, transcript revision, silence plan and reviewed decisions, director revision, brand revision, playbook revision, render ID and approved MP4 hash.

## Scorecard

These are editorial judgments. Use 1 for unusable, 2 for a major correction, 3 for acceptable with a minor correction, 4 for ready with a small observation, and 5 when the reviewed clip meets the check throughout. Leave a category unscored when evidence is missing.

| Category | Inspect | Evidence to record |
|---|---|---|
| Hook preservation | Meaning and audible opening | First spoken phrase, source and edited time, lost setup |
| Speech clarity | Intelligibility and complete words | Timecodes of unclear or clipped speech |
| Caption accuracy | Verbatim wording and displayed timing | Wrong, missing, or added words and cue offsets |
| Caption readability | Size, lines, contrast, script rendering | Mobile viewport and any overlapping or misplaced cues |
| Pacing | Natural breath and reflective pauses | Reviewed seam and reason for keeping or removing the pause |
| Audio continuity | Consistent voice and seamless cuts | Audible clicks, level jumps, silence or clipping |
| Framing | Face, headroom, safe margins and geometry | Crop or fit mode, face clipping, bars and caption overlap |
| Visual consistency | Stable composition and restrained emphasis | Unwanted jumps, colours, or unreadable text |
| Export reliability | Playback, approval, duration and file identity | Browser, codecs, stream timing, and SHA-256 |

For the initial pilot, an editorial pass requires every category to reach 4 and no clipped words, unplayable exports, or stale approvals. Keep any exceptions and the responsible reviewer in the scorecard.

## Benchmark procedure

Keep the input bytes, transcript, framing, compiled filters, caption asset, range, frame rate and audio settings fixed when comparing encoders. Time matched full outputs and repeat shorter controlled probes. Decode to FFmpeg's null sink with a negligible wrapper encoder, then add timeline processing, scale and fit, ASS, and AAC in stages. Use a separate cut-versus-no-cut window to estimate concatenation overhead. Subtract stage times as rough wall-clock deltas because decoding, filtering and encoding can overlap. Record the exact settings, sample window, repetitions and machine.

Validate the candidate outputs with FFprobe. Compare frames against the unencoded, composited reference, and inspect matched mobile frames for artifacts and text readability. PSNR measures pixel error and can support a comparison. Human review determines whether the resulting picture is acceptable. Retain a software control and avoid changing encoder defaults based only on a speed result.

## Apple M3 Pro performance control

The 74.3-second performance control had existing burned-in captions. All full runs used the same 1080 × 1920, 30 fps source, fit framing, no cuts, and the same ASS overlay. Intervals below are wall time. They exclude transcription and application-level review.

| Full render | Time | MP4 size | PSNR against composited reference, first 15 s |
|---|---:|---:|---:|
| x264 High, medium / CRF 17 | 63.37 s | 76.54 MB | 47.77 dB |
| VideoToolbox H.264 High, target 8.04 Mb/s | 10.89 s | 77.17 MB | 45.02 dB |
| x264 Standard, veryfast / CRF 22 | 15.59 s | 33.51 MB | 43.83 dB |

The second full software High run took 61.05 s with the same output size. The 15-second repeats took 13.85 and 16.70 s for x264 High, and 2.36 and 2.39 s for VideoToolbox. FFprobe confirmed H.264/yuv420p and AAC for every full run. Standard used AAC 160 kb/s. The matched High and hardware comparison used AAC 192 kb/s. Software High met the target on footage longer than one minute and gave the highest PSNR, so the production encoder defaults remain unchanged.

| 15-second probe | Run 1 | Run 2 |
|---|---:|---:|
| Input decoding to null | 1.10 s | 1.12 s |
| Decode plus timing and trimming | 1.16 s | 1.37 s |
| Plus scaling and fit | 1.32 s | 1.42 s |
| Separate concat probe with two short exclusions | 1.34 s | 1.44 s |
| Uncut scaling plus ASS | 1.32 s | 1.26 s |
| ASS plus AAC, video sent to null | 1.21 s | 1.20 s |
| Full x264 High encode | 13.85 s | 16.70 s |

ASS and AAC increments were too small to separate from run-to-run noise on this footage. The concat probe serves as an approximate bound because it processes a different set of frames and reads slightly farther into the source. Video encoding dominated this control. These results describe the tested source, composition, and playback format. HDR tone mapping and different source resolution can change the balance.
