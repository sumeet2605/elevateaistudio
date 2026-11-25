# Synthetic footage

The one-second fixtures were generated from FFmpeg's built-in color pattern and a 440 Hz tone. They contain no customer footage.

```sh
ffmpeg -f lavfi -i 'testsrc2=size=160x90:rate=24' \
  -f lavfi -i 'sine=frequency=440:sample_rate=48000' \
  -t 1 -c:v libx264 -pix_fmt yuv420p -c:a aac -movflags +faststart studio-synthetic.mp4
ffmpeg -i studio-synthetic.mp4 -c copy -movflags +faststart studio-synthetic.mov
```

`studio-spoken.mp4` pairs generated color bars with synthesized English speech. It contains no customer media. To reproduce it on macOS with the Samantha voice installed:

```sh
say -v Samantha -r 155 -o studio-voice.aiff "Growth starts with one small decision. Take a breath and notice what matters. Then choose one action you can take today. You can build momentum by keeping that promise to yourself."
ffmpeg -f lavfi -i 'testsrc2=size=320x180:rate=15' -i studio-voice.aiff -shortest -c:v libx264 -pix_fmt yuv420p -crf 30 -c:a aac -b:a 56k -movflags +faststart studio-spoken.mp4
```

`studio-paused.mp4` pairs generated color bars with four synthesized English phrases. The mono 16 kHz PCM has 1.85 s of leading silence, two long pauses of 2.65 s and 2.30 s, a short 0.17 s pause, and 2.5 s of trailing silence before AAC encoding. It contains no customer footage. The phrases are from the Day 2 fixture, synthesized separately so the pauses can be controlled. The fast test suite also generates a deterministic stereo WAV with opposite-phase tones, quiet tones, seeded noise, short and long gaps, and full-scale samples.
