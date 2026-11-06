import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


def timestamp(seconds):
    minutes, milliseconds = divmod(round(seconds * 1000), 60000)
    return f"{minutes:02}:{milliseconds / 1000:06.3f}"


def parse_time(value):
    parts = [float(part) for part in value.strip().split(":")]
    if len(parts) > 3 or any(not math.isfinite(part) or part < 0 for part in parts):
        raise ValueError("Enter a time in seconds or MM:SS.mmm.")
    return sum(part * 60 ** i for i, part in enumerate(reversed(parts)))


def run(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=True).stdout


EXPORT_QUALITY = {"Compact": "24", "High": "18", "Maximum": "14"}


def export_clip(source, output, start, end, layout, captions, audio_mode="Original", quality="High"):
    audio_args = ["-an"]
    if audio_mode != "Mute":
        audio_args = ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "192k"]
        if audio_mode == "Normalize":
            audio_args.extend(["-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-ar", "48000"])
    filters = []
    if layout in ("Vertical fit", "Vertical crop", "Square fit", "Square crop"):
        height = 1080 if layout.startswith("Square") else 1920
        if layout.endswith("fit"):
            filters.extend([
                f"scale=1080:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2",
                f"pad=1080:{height}:(ow-iw)/2:(oh-ih)/2",
            ])
        else:
            filters.extend([
                f"scale=1080:{height}:force_original_aspect_ratio=increase:force_divisible_by=2",
                f"crop=1080:{height}",
            ])
    else:
        filters.append("pad=ceil(iw/2)*2:ceil(ih/2)*2")
    filters.append("setsar=1")
    with tempfile.TemporaryDirectory() as temp:
        if captions:
            shutil.copyfile(captions, Path(temp) / "captions.srt")
            filters.append("subtitles=captions.srt:force_style='FontSize=20,Outline=2,Alignment=2,MarginV=28'")
        with tempfile.TemporaryDirectory(dir=Path(output).parent) as staging:
            pending = Path(staging) / "clip.mp4"
            run("ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                "-ss", str(start), "-i", str(source), "-t", str(end - start),
                "-map", "0:V:0", *audio_args, "-vf", ",".join(filters),
                "-c:v", "libx264", "-preset", "fast", "-crf", EXPORT_QUALITY[quality], "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                str(pending), cwd=temp)
            pending.replace(output)


def transcribe_clip(source, output, start, end, executable, model):
    with tempfile.TemporaryDirectory(dir=output.parent) as staging:
        audio = Path(staging) / "speech.wav"
        subtitle_base = Path(staging) / "captions"
        run("ffmpeg", "-nostdin", "-loglevel", "error", "-ss", str(start),
            "-i", str(source), "-t", str(end - start), "-map", "0:a:0",
            "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio))
        run(str(executable), "-m", str(model), "-f", str(audio),
            "-l", "auto", "-osrt", "-of", str(subtitle_base))
        srt = subtitle_base.with_suffix(".srt")
        if not srt.is_file() or "-->" not in srt.read_text(encoding="utf-8-sig"):
            raise ValueError("Whisper returned no captions for this cut.")
        srt.replace(output)


def detect_highlights(source, duration, clip_length=15):
    analysis = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "info", "-i", str(source),
         "-map", "0:V:0", "-an",
         "-vf", r"fps=4,scale=320:-2,select=gt(scene\,0.18),showinfo",
         "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
    scores = {}
    for moment in re.findall(r"pts_time:([0-9.]+)", analysis.stderr):
        second = int(float(moment))
        scores[second] = min(0.6, scores.get(second, 0) + 0.2)

    # Audio is optional, including for screen recordings with no audio track.
    streams = run("ffprobe", "-v", "error", "-select_streams", "a:0",
                  "-show_entries", "stream=index", "-of", "csv=p=0", str(source))
    if streams.strip():
        with tempfile.TemporaryDirectory() as temp:
            audio = Path(temp) / "input.wav"
            run("ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(source),
                "-map", "0:a:0", "-ac", "1", "-ar", "8000", str(audio))
            report = run("ffprobe", "-v", "error", "-f", "lavfi", "-i",
                         "amovie=input.wav,asetnsamples=n=8000:p=0,astats=metadata=1:reset=1",
                         "-show_entries", "frame=pts_time:frame_tags=lavfi.astats.Overall.RMS_level",
                         "-of", "json", cwd=temp)
        levels = {}
        for frame in json.loads(report).get("frames", []):
            level = float(frame.get("tags", {}).get("lavfi.astats.Overall.RMS_level", "-inf"))
            if "pts_time" in frame and math.isfinite(level):
                levels[int(float(frame["pts_time"]))] = level
        if levels:
            baseline = sorted(levels.values())[len(levels) // 2]
            for second, level in levels.items():
                if level >= -55 and level >= baseline + 3:
                    scores[second] = scores.get(second, 0) + min(1, (level - baseline) / 15)

    picks = []
    for moment in sorted(scores, key=scores.get, reverse=True):
        if len(picks) == 10:
            break
        start = max(0, min(moment - clip_length / 2, duration - clip_length))
        end = min(duration, start + clip_length)
        if all(end <= left or start >= right for left, right in picks):
            picks.append((start, end))
    return sorted(picks)


class Clipper:
    def __init__(self, root):
        self.root = root
        self.source = self.captions = self.player = None
        self.preview_folder = None
        self.whisper_model = None
        self.generated_selection = None
        self.duration = 0
        self.picks = []
        self.start = tk.StringVar(value="00:00.000")
        self.end = tk.StringVar(value="00:00.000")
        self.layout = tk.StringVar(value="Original")
        self.audio_mode = tk.StringVar(value="Original")
        self.quality = tk.StringVar(value="High")
        self.highlight_length = tk.StringVar(value="15")
        self.status = tk.StringVar(value="Open a video to begin.")
        root.title("Cutroom | Video Clipper")
        root.minsize(660, 400)
        root.protocol("WM_DELETE_WINDOW", self.close)
        pane = ttk.Frame(root, padding=24)
        pane.pack(fill="both", expand=True)
        ttk.Label(pane, text="Cutroom", font=("Segoe UI", 24, "bold")).pack(anchor="w")
        ttk.Label(pane, text="Trim recordings, find highlights, and export clips.").pack(anchor="w", pady=(0, 14))
        self.actions = []
        row = ttk.Frame(pane)
        row.pack(fill="x")
        for label, action in (("Open video", self.open), ("Find highlights", self.scan),
                              ("Preview export", self.preview_export), ("Export MP4", self.export)):
            button = ttk.Button(row, text=label, command=action)
            button.pack(side="left", padx=(0, 8))
            self.actions.append(button)
        self.position = ttk.Label(pane, text="00:00.000")
        self.position.pack(anchor="w", pady=(16, 0))
        self.timeline = ttk.Scale(pane, from_=0, to=1,
                                  command=lambda value: self.position.configure(text=timestamp(float(value))))
        self.timeline.pack(fill="x")
        for key, delta in (("Left", -0.1), ("Right", 0.1),
                           ("Shift-Left", -1), ("Shift-Right", 1)):
            self.timeline.bind(f"<{key}>", lambda event, step=delta: self.nudge_playhead(step))
        self.timeline.bind("<Key-i>", lambda event: self.mark_boundary(self.start))
        self.timeline.bind("<Key-o>", lambda event: self.mark_boundary(self.end))
        ttk.Label(pane, text="Focused timeline: arrows ±0.1 s · Shift+arrows ±1 s · I/O mark cut").pack(anchor="w")
        row = ttk.Frame(pane)
        row.pack(fill="x", pady=10)
        for name, value in (("In", self.start), ("Out", self.end)):
            ttk.Button(row, text=f"Set {name}", command=lambda v=value: self.mark_boundary(v)).pack(side="left")
            ttk.Entry(row, textvariable=value, width=12).pack(side="left", padx=(4, 10))
        ttk.Combobox(row, state="readonly", textvariable=self.layout,
                     values=("Original", "Vertical fit", "Vertical crop", "Square fit", "Square crop"), width=14).pack(side="left")
        ttk.Button(row, text="Preview playhead", command=lambda: self.preview(True)).pack(side="left", padx=6)
        row = ttk.Frame(pane)
        row.pack(fill="x")
        ttk.Button(row, text="Add SRT captions", command=self.choose_captions).pack(side="left")
        generate = ttk.Button(row, text="Generate captions", command=self.generate_captions)
        generate.pack(side="left", padx=(6, 0))
        self.actions.append(generate)
        ttk.Button(row, text="Clear captions", command=self.clear_captions).pack(side="left", padx=6)
        self.caption_label = ttk.Label(row, text="SRT timecodes are relative to the clip.")
        self.caption_label.pack(side="left", fill="x", expand=True)
        self.start.trace_add("write", self.invalidate_generated_captions)
        self.end.trace_add("write", self.invalidate_generated_captions)
        ttk.Button(row, text="Stop playback", command=self.stop_playback).pack(side="right")
        scan_options = ttk.Frame(pane)
        scan_options.pack(fill="x", pady=(10, 0))
        ttk.Label(scan_options, text="Highlight length (seconds)").pack(side="left")
        ttk.Spinbox(scan_options, from_=3, to=120, width=6,
                    textvariable=self.highlight_length).pack(side="left", padx=6)
        ttk.Label(scan_options, text="Export audio").pack(side="left", padx=(16, 0))
        ttk.Combobox(scan_options, state="readonly", textvariable=self.audio_mode,
                     values=("Original", "Normalize", "Mute"), width=12).pack(side="left", padx=6)
        ttk.Label(scan_options, text="Video quality").pack(side="left", padx=(16, 0))
        ttk.Combobox(scan_options, state="readonly", textvariable=self.quality,
                     values=tuple(EXPORT_QUALITY), width=10).pack(side="left", padx=6)
        self.results = tk.Listbox(pane, height=7)
        self.results.pack(fill="both", expand=True, pady=12)
        self.results.bind("<<ListboxSelect>>", self.select_pick)
        ttk.Label(pane, textvariable=self.status, wraplength=610).pack(anchor="w")
        self.busy = False

    def background(self, label, operation, done):
        if self.busy:
            return
        self.busy = True
        self.status.set(label)
        for button in self.actions:
            button.state(["disabled"])

        def finish(value, error):
            self.busy = False
            for button in self.actions:
                button.state(["!disabled"])
            if error:
                self.status.set("Operation failed.")
                messagebox.showerror("Cutroom", error)
            else:
                done(value)

        def worker():
            try:
                value = operation()
            except Exception as exc:
                detail = getattr(exc, "stderr", None) or str(exc)
                self.root.after(0, finish, None, detail.strip()[-1800:])
            else:
                self.root.after(0, finish, value, None)

        threading.Thread(target=worker, daemon=True).start()

    def open(self):
        if not all(shutil.which(tool) for tool in ("ffmpeg", "ffprobe")):
            messagebox.showerror("Cutroom", "Install FFmpeg and add ffmpeg and ffprobe to PATH.")
            return
        selected = filedialog.askopenfilename(title="Open a local video")
        if not selected:
            return
        source = Path(selected).resolve()

        def probe():
            info = json.loads(run("ffprobe", "-v", "error", "-select_streams", "V:0",
                                  "-show_entries", "format=duration:stream=index", "-of", "json", str(source)))
            duration = float(info["format"]["duration"])
            if not info["streams"] or not math.isfinite(duration) or duration <= 0:
                raise ValueError("Choose a video with a known duration.")
            return duration

        def loaded(duration):
            self.stop_preview()
            self.source, self.duration = source, duration
            self.clear_captions()
            self.start.set(timestamp(0))
            self.end.set(timestamp(math.floor(duration * 1000) / 1000))
            self.timeline.configure(to=duration)
            self.timeline.set(0)
            self.results.delete(0, "end")
            self.picks = []
            self.status.set(f"{source.name}  |  {timestamp(duration)}")

        self.background("Reading video…", probe, loaded)

    def nudge_playhead(self, step):
        if self.source is not None:
            position = max(0, min(self.duration, self.timeline.get() + step))
            self.timeline.set(position)
        return "break"

    def mark_boundary(self, boundary):
        if self.source is not None:
            boundary.set(timestamp(self.timeline.get()))
        return "break"

    def cut(self):
        if self.source is None:
            raise ValueError("Open a video first.")
        start, end = parse_time(self.start.get()), parse_time(self.end.get())
        if not 0 <= start < end <= self.duration + 0.0005:
            raise ValueError("Set In and Out within the video, with Out after In.")
        return start, min(end, self.duration)

    def scan(self):
        if self.source is None:
            messagebox.showinfo("Cutroom", "Open a video first.")
            return
        try:
            clip_length = int(self.highlight_length.get())
            if not 3 <= clip_length <= 120:
                raise ValueError
        except ValueError:
            messagebox.showerror("Cutroom", "Enter a whole number from 3 to 120 seconds.")
            return
        source, duration = self.source, self.duration

        def display(picks):
            self.picks = picks
            self.results.delete(0, "end")
            for start, end in picks:
                self.results.insert("end", f"Highlight   {timestamp(start)}  →  {timestamp(end)}")
            self.status.set(f"{len(picks)} suggestions. Select one to set the cut.")

        self.background("Scanning video and audio…", lambda: detect_highlights(source, duration, clip_length), display)

    def select_pick(self, _event):
        selection = self.results.curselection()
        if selection:
            start, end = self.picks[selection[0]]
            self.start.set(timestamp(start))
            self.end.set(timestamp(end))
            self.timeline.set(start)

    def generate_captions(self):
        try:
            start, end = self.cut()
            executable = shutil.which("whisper-cli")
            if not executable:
                raise ValueError("Install whisper.cpp and add whisper-cli to PATH. See the README.")
        except ValueError as exc:
            messagebox.showerror("Cutroom", str(exc))
            return
        if self.whisper_model is None:
            model = filedialog.askopenfilename(title="Choose a Whisper GGML model",
                                               filetypes=[("Whisper model", "*.bin")])
            if not model:
                return
            self.whisper_model = Path(model).resolve()
        source = self.source
        selected = filedialog.asksaveasfilename(
            title="Save clip captions", initialfile=f"{source.stem}-clip.srt",
            defaultextension=".srt", filetypes=[("SubRip captions", "*.srt")])
        if not selected:
            return
        output = Path(selected).resolve()
        if output == source:
            messagebox.showerror("Cutroom", "Choose a different file from the source video.")
            return

        def ready(_):
            if (self.start.get(), self.end.get()) == selection:
                self.generated_selection = selection
                self.captions = output
                self.caption_label.configure(text=output.name)
            self.status.set(f"Saved {output}. Captions are timed to the cut used to generate them.")

        selection = self.start.get(), self.end.get()
        self.background("Generating captions locally…",
                        lambda: transcribe_clip(source, output, start, end, executable, self.whisper_model),
                        ready)

    def choose_captions(self):
        path = filedialog.askopenfilename(filetypes=[("SubRip captions", "*.srt")])
        if path:
            self.generated_selection = None
            self.captions = Path(path).resolve()
            self.caption_label.configure(text=self.captions.name)

    def invalidate_generated_captions(self, *_):
        if self.generated_selection and (self.start.get(), self.end.get()) != self.generated_selection:
            self.clear_captions()

    def clear_captions(self):
        self.generated_selection = None
        self.captions = None
        self.caption_label.configure(text="SRT timecodes are relative to the clip.")

    def stop_playback(self):
        if not self.busy:
            self.stop_preview()

    def stop_preview(self):
        if self.player and self.player.poll() is None:
            self.player.terminate()
            self.player.wait()
        if self.preview_folder:
            self.preview_folder.cleanup()
            self.preview_folder = None

    def preview_export(self):
        try:
            start, end = self.cut()
            if not shutil.which("ffplay"):
                raise ValueError("Add ffplay to PATH to preview exports.")
        except ValueError as exc:
            messagebox.showerror("Cutroom", str(exc))
            return
        self.stop_preview()
        source, layout, captions = self.source, self.layout.get(), self.captions
        audio_mode = self.audio_mode.get()
        quality = self.quality.get()
        self.preview_folder = tempfile.TemporaryDirectory()
        output = Path(self.preview_folder.name) / "preview.mp4"

        def play(_):
            try:
                self.player = subprocess.Popen(["ffplay", "-autoexit", "-loglevel", "error", str(output)])
                self.status.set("Playing the first 8 seconds of the export.")
            except OSError as exc:
                self.stop_preview()
                self.status.set(str(exc))

        self.background("Rendering export preview…",
                        lambda: export_clip(source, output, start, min(end, start + 8), layout, captions, audio_mode, quality),
                        play)

    def preview(self, playhead=False):
        if self.busy:
            return
        try:
            if playhead:
                if self.source is None:
                    raise ValueError("Open a video first.")
                start, end = self.timeline.get(), self.duration
            else:
                start, end = self.cut()
            self.stop_preview()
            self.player = subprocess.Popen(["ffplay", "-autoexit", "-loglevel", "error",
                                            "-ss", str(start), "-t", str(end - start), str(self.source)])
        except (ValueError, OSError) as exc:
            messagebox.showerror("Cutroom", str(exc))

    def export(self):
        try:
            start, end = self.cut()
        except ValueError as exc:
            messagebox.showerror("Cutroom", str(exc))
            return
        selected = filedialog.asksaveasfilename(defaultextension=".mp4",
                                               initialfile=f"{self.source.stem}-clip.mp4",
                                               filetypes=[("MP4 video", "*.mp4")])
        if selected:
            source, output = self.source, Path(selected).resolve()
            if source == output:
                messagebox.showerror("Cutroom", "Choose a different file from the source video.")
                return
            layout, captions = self.layout.get(), self.captions
            audio_mode = self.audio_mode.get()
            quality = self.quality.get()
            self.background("Exporting MP4…", lambda: export_clip(source, output, start, end, layout, captions, audio_mode, quality),
                            lambda _: self.status.set(f"Saved {output}"))

    def close(self):
        if self.busy:
            messagebox.showinfo("Cutroom", "Wait for the current operation to finish.")
            return
        self.stop_preview()
        self.root.destroy()


if __name__ == "__main__":
    if getattr(sys, "frozen", False):
        bundled_tools = Path(sys.executable).parent / "ffmpeg" / "bin"
        os.environ["PATH"] = str(bundled_tools) + os.pathsep + os.environ.get("PATH", "")
    window = tk.Tk()
    Clipper(window)
    window.mainloop()











