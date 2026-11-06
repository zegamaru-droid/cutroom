# Cutroom

Cutroom is a local desktop video clipper for gameplay, podcasts, streams, and other recordings. It works with formats your FFmpeg build can decode.

## Run on Windows

Install Python with Tkinter and an FFmpeg build that includes `ffmpeg`, `ffprobe`, `ffplay`, libx264, and libass. Add the FFmpeg executables to PATH, then run this from the video-clipper folder.

```powershell
python clipper.py
```

1. Open a recording.
2. Move the playhead and click **Set In** or **Set Out**. You can also enter times directly in seconds or as `MM:SS.mmm`. With the timeline focused, use the arrow keys to move by 0.1 seconds, Shift+arrow to move by 1 second, and I/O to mark the cut boundaries. **Preview playhead** opens source playback at the playhead.
3. Set **Highlight length** (3–120 seconds, default 15), then use **Find highlights** to suggest up to ten cuts using visual changes and audio peaks relative to the recording. Silent videos use visual changes alone. Click a suggestion to edit or export it.
4. Choose **Original** or a **Vertical** or **Square** layout. Fit preserves the whole frame. Crop fills the frame with a centered crop.
5. Optionally import an SRT file with timecodes measured from the start of the clip. Captions are burned into the export.
6. Export an H.264 MP4. Vertical exports are 1080 × 1920. Square exports are 1080 × 1080.

**Preview export** renders up to eight seconds of the cut with the selected framing and captions, then opens it in a separate player. Use **Stop playback** to close the player.





Use **Export audio** to keep the source audio, normalize loudness toward -16 LUFS, or mute the clip. The export preview uses the same setting.

**Video quality** applies to the preview and export. **Compact** favors a smaller file, **High** keeps the default quality, and **Maximum** retains more detail at a larger file size. Output size depends on the footage.

## Automatic captions

Install [whisper.cpp](https://github.com/ggml-org/whisper.cpp), add its `whisper-cli` executable to PATH, and download a GGML Whisper model. Set the clip boundaries and choose **Generate captions**, then select the model and save an SRT. Transcription runs locally. Cutroom loads the resulting SRT for preview and export when the selected cut has stayed the same. If you change the cut, regenerate captions to match the new boundaries.
