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


def export_clip(source, output, start, end, layout, captions):
    filters = []
    if layout == "Vertical fit":
        filters.extend(["scale=1080:1920:force_original_aspect_ratio=decrease:force_divisible_by=2",
                        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2"])
    elif layout == "Vertical crop":
        filters.extend(["scale=1080:1920:force_original_aspect_ratio=increase:force_divisible_by=2",
                        "crop=1080:1920"])
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
                "-map", "0:V:0", "-map", "0:a:0?", "-vf", ",".join(filters),
                "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                str(pending), cwd=temp)
            pending.replace(output)


def detect_highlights(source, duration):
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
        start = max(0, min(moment - 7, duration - 15))
        end = min(duration, start + 15)
        if all(end <= left or start >= right for left, right in picks):
            picks.append((start, end))
    return sorted(picks)


class Clipper:
    def __init__(self, root):
        self.root = root
        self.source = self.captions = self.player = None
        self.preview_folder = None
        self.duration = 0
        self.picks = []
        self.start = tk.StringVar(value="00:00.000")
        self.end = tk.StringVar(value="00:00.000")
        self.layout = tk.StringVar(value="Original")
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
        row = ttk.Frame(pane)
        row.pack(fill="x", pady=10)
        for name, value in (("In", self.start), ("Out", self.end)):
            ttk.Button(row, text=f"Set {name}", command=lambda v=value: v.set(
                timestamp(self.timeline.get()))).pack(side="left")
            ttk.Entry(row, textvariable=value, width=12).pack(side="left", padx=(4, 10))
        ttk.Combobox(row, state="readonly", textvariable=self.layout,
                     values=("Original", "Vertical fit", "Vertical crop"), width=14).pack(side="left")
        ttk.Button(row, text="Preview playhead", command=lambda: self.preview(True)).pack(side="left", padx=6)
        row = ttk.Frame(pane)
        row.pack(fill="x")
        ttk.Button(row, text="Add SRT captions", command=self.choose_captions).pack(side="left")
        ttk.Button(row, text="Clear captions", command=self.clear_captions).pack(side="left", padx=6)
        self.caption_label = ttk.Label(row, text="SRT timecodes are relative to the clip.")
        self.caption_label.pack(side="left")
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
        source, duration = self.source, self.duration

        def display(picks):
            self.picks = picks
            self.results.delete(0, "end")
            for start, end in picks:
                self.results.insert("end", f"Highlight   {timestamp(start)}  →  {timestamp(end)}")
            self.status.set(f"{len(picks)} suggestions. Select one to set the cut.")

        self.background("Scanning video and audio…", lambda: detect_highlights(source, duration), display)

    def select_pick(self, _event):
        selection = self.results.curselection()
        if selection:
            start, end = self.picks[selection[0]]
            self.start.set(timestamp(start))
            self.end.set(timestamp(end))
            self.timeline.set(start)

    def choose_captions(self):
        path = filedialog.askopenfilename(filetypes=[("SubRip captions", "*.srt")])
        if path:
            self.captions = Path(path).resolve()
            self.caption_label.configure(text=self.captions.name)

    def clear_captions(self):
        self.captions = None
        self.caption_label.configure(text="SRT timecodes are relative to the clip.")

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
                        lambda: export_clip(source, output, start, min(end, start + 8), layout, captions),
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
            self.background("Exporting MP4…", lambda: export_clip(source, output, start, end, layout, captions),
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




