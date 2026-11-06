# Cutroom

Cutroom is a local desktop video clipper for gameplay, podcasts, streams, and other recordings. It works with formats your FFmpeg build can decode.

## Run on Windows

Install Python with Tkinter and an FFmpeg build that includes `ffmpeg`, `ffprobe`, `ffplay`, libx264, and libass. Add the FFmpeg executables to PATH, then run this from the video-clipper folder.

```powershell
python clipper.py
```

1. Open a recording.
2. Move the playhead and click **Set In** or **Set Out**. You can also enter times directly in seconds or as `MM:SS.mmm`. **Preview playhead** opens source playback at the playhead.
3. Use **Find highlights** to suggest up to ten 15-second cuts using visual changes and audio peaks relative to the recording. Silent videos use visual changes alone. Click a suggestion to edit or export it.
4. Choose **Original**, **Vertical fit** (keep the whole frame), or **Vertical crop** (fill the frame with a centered crop).
5. Optionally import an SRT file with timecodes measured from the start of the clip. Captions are burned into the export.
6. Export an H.264 MP4. Vertical exports are 1080 × 1920.

**Preview export** renders up to eight seconds of the cut with the selected framing and captions, then opens it in a separate player.

