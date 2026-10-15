#!/usr/bin/env python3
"""video_chuan.py - xu ly video 9:16 hang loat bang ffmpeg.

Vi du:
    python3 video_chuan.py input/ -o output/ -j 4 --flip --bgm nhac/

Yeu cau: ffmpeg + ffprobe (ban co libx264, libass) trong PATH. Chi dung thu vien chuan Python.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import functools
import json
import math
import os
import queue
import random
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from array import array
from dataclasses import dataclass, field
from operator import mul
from pathlib import Path

try:  # giao dien (khong bat buoc: thieu tkinter thi van chay bang dong lenh)
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from tkinter import font as tkfont
except ImportError:
    tk = None

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".flv", ".ts", ".wmv"}
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
BG_LAYER_OPACITY = (1.0, 0.5, 0.3)  # 3 lop anh nen: duoi cung -> tren

FPS = "30000/1001"  # 29.97 FPS (gia tri NTSC chuan)
SAMPLE_RATE = 44100
TEMP_SUFFIXES = (".filter.txt", ".subs.ass", ".part.mp4")

# Hieu ung FX chen theo chu ky ngau nhien
FX_TYPES = ("flash", "punch", "glitch")
FLASH_DUR, FLASH_BRIGHTNESS = 0.12, 0.07
GLITCH_DUR, GLITCH_SHIFT = 0.15, 6
PUNCH_ZOOM, PUNCH_SIGMA = 0.03, 0.08

# Phan tich am thanh (silence cut / energy hook)
ENERGY_RATE = 8000   # Hz, PCM mono dung de do nang luong
ENERGY_BLOCK = 0.1   # giay moi block nang luong
AFMT = f"aresample={SAMPLE_RATE},aformat=sample_fmts=fltp:channel_layouts=stereo"


# --------------------------------------------------------------------------- #
# Cau hinh
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    width: int = 1080
    height: int = 1920
    safe_w: int = 880
    safe_h: int = 1420
    speed: float = 1.065
    zoom_min: float = 1.04
    zoom_max: float = 1.08
    zoom_period: tuple[float, float] = (4.0, 6.0)
    grain: float = 0.045
    fx_interval: tuple[float, float] | None = (3.0, 6.0)
    rotate_deg: float = 0.7
    flip: bool = False
    saturation: float = 1.12
    contrast: float = 1.18
    brightness: float = -0.04
    # Ep tong nga lanh: day xanh nhe, ha vang nong (s = shadows, h = highlights)
    colorbalance: str = "rs=-0.06:gs=0.01:bs=0.10:rh=-0.05:gh=0.02:bh=0.08"
    hue_range: float = 4.0
    drop_every: int = 145
    ghost: bool = True
    ghost_size: int = 150
    ghost_alpha: float = 0.015
    blur_box: tuple[float, float, float, float] | None = (0.0, 0.75, 1.0, 0.2)
    blur_radius: int = 20
    fit: str = "auto"
    noise_amp: float = 0.02
    bgm_volume: tuple[float, float] = (0.03, 0.05)
    bgm_fade: float = 1.5
    bitrate_mbps: tuple[float, float] = (5.7, 6.3)
    maxrate_mbps: float = 8.0
    audio_bitrate: str = "192k"
    preset: str = "medium"
    threads: int = 0  # 0 = de ffmpeg tu chon
    silence_cut: bool = False
    silence_db: float = -35.0
    silence_min: float = 0.5
    silence_pad: float = 0.15
    hook_mode: str = "off"  # off | move | copy
    hook_len: float = 3.0
    micro_cut: bool = True        # cu moi micro_period giay bo micro_len giay
    micro_period: float = 1.2
    micro_len: float = 0.1
    ambient_files: list[Path] = field(default_factory=list)  # thay pink noise + BGM
    ambient_volume: tuple[float, float] = (0.02, 0.03)
    bg_files: list[Path] = field(default_factory=list)       # 3 lop anh nen duoi video
    bg_scale: float = 0.85        # video chiem 85% khung khi co anh nen
    overlay_on: bool = True       # lop phu nga lanh tren cung
    overlay_file: Path | None = None  # texture 4K (anh/video); None = tu sinh lop mau
    overlay_color: str = "0xEEF1F4"   # nga lanh
    overlay_opacity: float = 0.06

    @property
    def fast_rate(self) -> int:
        """Sample rate cho asetrate: 44100 * speed."""
        return int(SAMPLE_RATE * self.speed + 0.5)


class MicroCut:
    """Cat nhip tren timeline 29.97 FPS: bo frame k neu (k*1001) mod P > C.

    P = chu ky, C = phan giu lai, ca hai tinh theo don vi 1/30000 giay (frame k bat dau o
    k*1001/30000 s). Tuong duong select='not(mod(t,1.2)>1.1)' nhung tinh bang so nguyen nen
    video va audio luon bo dung cung mot doan.
    """

    FADE = 0.006  # giay lam mem 2 ben moi diem cat audio (chong tieng 'tach')

    def __init__(self, period: float, length: float):
        self.P = round(period * 30000)
        self.C = round((period - length) * 30000)
        self._prefix = [0]

    def keep(self, k: int) -> bool:
        return (k * 1001) % self.P <= self.C

    def kept_before(self, k: int) -> int:
        """So frame duoc giu trong [0, k)."""
        while len(self._prefix) <= k:
            j = len(self._prefix) - 1
            self._prefix.append(self._prefix[-1] + self.keep(j))
        return self._prefix[k]

    def map_time(self, t: float) -> float:
        """Thoi gian truoc cat -> sau cat (diem nam trong doan bi bo thi dinh vao cho noi)."""
        x = max(0.0, t) * 30000 / 1001
        k = int(x)
        return (self.kept_before(k) + ((x - k) if self.keep(k) else 0.0)) * 1001 / 30000

    def video_select(self) -> str:
        return f"lte(mod(n*1001,{self.P}),{self.C})"

    def audio_select(self) -> str:
        return f"lte(mod(floor(t*30000/1001+1e-9)*1001,{self.P}),{self.C})"

    def declick(self) -> str:
        """He so am luong: giam ve 0 ngay truoc doan bi bo va tang lai ngay sau doan bi bo."""
        P, C = self.P, self.C
        return (f"st(0,floor(t*30000/{P}));"
                f"st(1,(floor(({P}*ld(0)+{C})/1001)+1)*1001/30000);"   # dau doan bi bo cua chu ky nay
                f"st(2,(floor(({P}*ld(0)-1)/1001)+1)*1001/30000);"     # cuoi doan bi bo cua chu ky truoc
                f"clip(min(ld(1)-t,t-ld(2))/{self.FADE},0,1)")


def output_frames(cfg: Config, src_duration: float) -> tuple[int, int]:
    """(so frame truoc cat nhip, so frame xuat ra) cua video 29.97 FPS."""
    frames = max(1, round(src_duration / cfg.speed * 30000 / 1001))
    if not cfg.micro_cut:
        return frames, frames
    return frames, max(1, MicroCut(cfg.micro_period, cfg.micro_len).kept_before(frames))


@dataclass
class MediaInfo:
    duration: float
    width: int
    height: int
    has_audio: bool


@dataclass
class Edit:
    """Danh sach doan giu lai (thoi gian goc, theo thu tu). Hook (neu co) dat len dau video."""
    segments: list[tuple[float, float]]
    hook: tuple[float, float] | None = None
    hook_mode: str = "off"
    cuts: int = 0          # so khoang lang da cat
    cut_seconds: float = 0.0

    @property
    def duration(self) -> float:
        hook = self.hook[1] - self.hook[0] if self.hook else 0.0
        return sum(b - a for a, b in self.segments) + hook

    def output_order(self) -> list[tuple[float, float]]:
        return ([self.hook] if self.hook else []) + self.segments


@dataclass
class Params:
    """Cac tham so ngau nhien rieng cho tung file."""
    name: str
    hue: float
    zoom_period: float
    zoom_phase: float
    ghost_gx: float
    ghost_gy: float
    bitrate_kbps: int
    fx_events: list[tuple[float, str]] = field(default_factory=list)
    bgm: Path | None = None
    bgm_volume: float = 0.0
    edit: Edit | None = None
    src_duration: float = 0.0  # thoi luong (giay) sau cat lang/hook, truoc khi tang toc
    ambient: list[tuple[Path, float, float]] = field(default_factory=list)  # (file, volume, giay bat dau)
    bg_images: list[Path] = field(default_factory=list)
    overlay_file: Path | None = None

    def input_keys(self) -> list[str]:
        """Thu tu cac input ffmpeg phu (input 0 luon la video goc)."""
        keys = ["hook"] if self.edit and self.edit.hook else []
        if self.bgm:
            keys.append("bgm")
        keys += [f"amb{i}" for i in range(len(self.ambient))]
        keys += [f"bg{i}" for i in range(len(self.bg_images))]
        if self.overlay_file:
            keys.append("tex")
        return keys

    def idx(self, key: str) -> int:
        return 1 + self.input_keys().index(key)


@dataclass
class Result:
    src: Path
    ok: bool
    output: Path | None = None
    params: Params | None = None
    error: str = ""
    elapsed: float = 0.0


# --------------------------------------------------------------------------- #
# Tien ich
# --------------------------------------------------------------------------- #
def even(x: float) -> int:
    return int(round(x / 2.0)) * 2


class Cancelled(RuntimeError):
    """Nguoi dung bam Dung."""


cancel_event = threading.Event()
_procs_lock = threading.Lock()
_procs: set[subprocess.Popen] = set()


def run_proc(cmd: list[str], cwd: Path | None = None, binary: bool = False,
             on_line=None) -> tuple[int, str | bytes, str]:
    """Chay lenh con (co the huy bang cancel_all). Tra ve (ma thoat, stdout, stderr).

    on_line: neu co, doc stdout tung dong (text) va goi on_line(dong) - dung cho -progress.
    """
    if cancel_event.is_set():
        raise Cancelled("da dung")
    enc = {} if binary else {"encoding": "utf-8", "errors": "replace"}
    proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, **enc)
    with _procs_lock:
        _procs.add(proc)
    try:
        if on_line is None:
            out, err = proc.communicate()
        else:
            err_parts: list[str] = []
            reader = threading.Thread(target=lambda: err_parts.append(proc.stderr.read()), daemon=True)
            reader.start()
            for line in proc.stdout:
                on_line(line)
            proc.wait()
            reader.join()
            out, err = "", "".join(err_parts)
    finally:
        with _procs_lock:
            _procs.discard(proc)
    if cancel_event.is_set():
        raise Cancelled("da dung")
    if binary:
        err = err.decode("utf-8", "replace")
    return proc.returncode, out, err


def cancel_all() -> None:
    """Dung moi ffmpeg/ffprobe dang chay; file tam se duoc process_one don dep."""
    cancel_event.set()
    with _procs_lock:
        for proc in list(_procs):
            try:
                proc.terminate()
            except OSError:
                pass


def probe(path: Path) -> MediaInfo:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries",
        "format=duration:stream=codec_type,width,height,duration"
        ":stream_tags:stream_side_data=rotation",
        "-of", "json", str(path),
    ]
    code, out, err = run_proc(cmd)
    if code != 0:
        raise RuntimeError(f"ffprobe loi: {err.strip()}")
    data = json.loads(out or "{}")
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise RuntimeError("file khong co luong video")
    width, height = int(video["width"]), int(video["height"])

    rotation = 0
    for sd in video.get("side_data_list", []) or []:
        if "rotation" in sd:
            rotation = int(float(sd["rotation"]))
    if not rotation and "rotate" in video.get("tags", {}):
        rotation = int(float(video["tags"]["rotate"]))
    if abs(rotation) % 180 == 90:  # ffmpeg tu xoay khi decode -> doi chieu
        width, height = height, width

    # Uu tien thoi luong cua luong VIDEO: neu audio goc dai hon video thi khong duoc keo dai theo audio
    duration = _num(video.get("duration")) or _tag_duration(video.get("tags", {})) \
        or _num(data.get("format", {}).get("duration"))
    if not duration:
        raise RuntimeError("khong doc duoc thoi luong video")
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    return MediaInfo(duration, width, height, has_audio)


def _num(value) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    return v if v > 0 and math.isfinite(v) else 0.0


def _tag_duration(tags: dict) -> float:
    """Tag DURATION cua mkv/webm, dang HH:MM:SS.nnnnnnnnn."""
    raw = next((v for k, v in tags.items() if k.upper() == "DURATION"), "")
    m = re.match(r"(\d+):(\d+):(\d+(?:\.\d+)?)", str(raw))
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else 0.0


_name_lock = threading.Lock()
_reserved_names: set[str] = set()


def unique_name(out_dir: Path) -> str:
    """Chuoi hex ngau nhien 8 ky tu, khong trung voi file da co trong thu muc xuat."""
    with _name_lock:
        while True:
            name = secrets.token_hex(4)
            if name in _reserved_names:
                continue
            if (out_dir / f"{name}.mp4").exists() or any(
                (out_dir / f"{name}{s}").exists() for s in TEMP_SUFFIXES
            ):
                continue
            _reserved_names.add(name)
            return name


# --------------------------------------------------------------------------- #
# Phu de: SRT -> ASS (dat trong safe zone, thoi gian chia theo speed)
# --------------------------------------------------------------------------- #
_TS = r"(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})"
_SRT_TIME = re.compile(_TS + r"\s*-->\s*" + _TS)


def _ts(groups) -> float:
    h, m, s, ms = groups
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0


def parse_srt(text: str) -> list[tuple[float, float, str]]:
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    cues = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.split("\n")
        for i, line in enumerate(lines):
            m = _SRT_TIME.search(line)
            if not m:
                continue
            g = m.groups()
            start, end = _ts(g[:4]), _ts(g[4:])
            body = "\n".join(lines[i + 1:]).strip()
            if body and end > start:
                cues.append((start, end, body))
            break
    return cues


def _ass_time(t: float) -> str:
    cs = max(0, int(round(t * 100)))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_text(body: str) -> str:
    body = body.replace("{", "(").replace("}", ")")
    for tag in ("i", "b", "u"):
        body = re.sub(rf"<{tag}>", rf"{{\\{tag}1}}", body, flags=re.I)
        body = re.sub(rf"</{tag}>", rf"{{\\{tag}0}}", body, flags=re.I)
    body = re.sub(r"<[^>]+>", "", body)
    return body.replace("\n", r"\N")


def build_ass(cues, cfg: Config) -> str:
    margin_lr = (cfg.width - cfg.safe_w) // 2
    margin_v = (cfg.height - cfg.safe_h) // 2
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {cfg.width}",
        f"PlayResY: {cfg.height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,Arial,64,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,"
        f"1,4,1,2,{margin_lr},{margin_lr},{margin_v},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    mc = MicroCut(cfg.micro_period, cfg.micro_len) if cfg.micro_cut else None
    for start, end, body in cues:
        s, e = start / cfg.speed, end / cfg.speed
        if mc:  # doi theo timeline sau khi cat nhip
            s, e = mc.map_time(s), mc.map_time(e)
        if e - s < 0.05:
            continue
        lines.append(f"Dialogue: 0,{_ass_time(s)},{_ass_time(e)},Default,,0,0,0,,{_ass_text(body)}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Tham so ngau nhien
# --------------------------------------------------------------------------- #
def fx_schedule(duration: float, interval: tuple[float, float] | None,
                rng: random.Random) -> list[tuple[float, str]]:
    """Moc chen FX: cach nhau ngau nhien trong [min, max] giay."""
    if not interval:
        return []
    lo, hi = interval
    events = []
    t = rng.uniform(lo, hi)
    while t < duration - 0.5:
        events.append((round(t, 3), rng.choice(FX_TYPES)))
        t += rng.uniform(lo, hi)
    return events


@functools.lru_cache(maxsize=256)
def media_duration(path: Path) -> float:
    """Thoi luong file am thanh/video (0 neu khong doc duoc)."""
    try:
        code, out, _ = run_proc(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                 "-of", "csv=p=0", str(path)])
    except (OSError, Cancelled):
        return 0.0
    return _num(out.strip()) if code == 0 else 0.0


def pick_ambient(cfg: Config, rng: random.Random) -> list[tuple[Path, float, float]]:
    """3 lop ambient khac nhau (thieu file thi dung lai file voi diem bat dau khac)."""
    files = cfg.ambient_files
    if not files:
        return []
    chosen = rng.sample(files, 3) if len(files) >= 3 else [files[i % len(files)] for i in range(3)]
    layers = []
    for f in chosen:
        dur = media_duration(f)
        start = rng.uniform(0, dur * 0.8) if dur > 1 else 0.0
        layers.append((f, round(rng.uniform(*cfg.ambient_volume), 4), round(start, 3)))
    return layers


def make_params(cfg: Config, src_duration: float, rng: random.Random, name: str,
                bgm_files: list[Path], edit: Edit | None = None) -> Params:
    out_duration = output_frames(cfg, src_duration)[1] * 1001 / 30000
    use_bgm = bool(bgm_files) and not cfg.ambient_files  # ambient thay the BGM
    return Params(
        edit=edit,
        src_duration=src_duration,
        ambient=pick_ambient(cfg, rng),
        bg_images=rng.sample(cfg.bg_files, min(3, len(cfg.bg_files))),
        overlay_file=cfg.overlay_file if cfg.overlay_on else None,
        name=name,
        hue=round(rng.uniform(-cfg.hue_range, cfg.hue_range), 2),
        zoom_period=round(rng.uniform(*cfg.zoom_period), 3),
        zoom_phase=round(rng.uniform(0, 2 * math.pi), 3),
        ghost_gx=round(rng.uniform(0.3, 0.9), 3),
        ghost_gy=round(rng.uniform(0.3, 0.9), 3),
        bitrate_kbps=int(round(rng.uniform(*cfg.bitrate_mbps) * 1000)),
        fx_events=fx_schedule(out_duration, cfg.fx_interval, rng),
        bgm=rng.choice(bgm_files) if use_bgm else None,
        bgm_volume=round(rng.uniform(*cfg.bgm_volume), 4) if use_bgm else 0.0,
    )


# --------------------------------------------------------------------------- #
# Silence Cut + Energy Hook
# --------------------------------------------------------------------------- #
def analyze_audio(src: Path, cfg: Config, duration: float) -> tuple[list[tuple[float, float]], list[float]]:
    """1 lan doc audio: silencedetect (log stderr) + PCM mono 8 kHz (stdout) de do nang luong."""
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-i", str(src), "-map", "0:a:0",
           "-af", f"silencedetect=noise={cfg.silence_db}dB:d={cfg.silence_min}",
           "-ac", "1", "-ar", str(ENERGY_RATE), "-f", "s16le", "pipe:1"]
    code, pcm, err = run_proc(cmd, binary=True)
    if code != 0:
        raise RuntimeError("phan tich audio loi: " + " | ".join(err.strip().splitlines()[-3:]))
    silences = parse_silences(err, duration)

    samples = array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    n = int(ENERGY_RATE * ENERGY_BLOCK)
    energy = []
    for i in range(0, len(samples) - n + 1, n):
        chunk = samples[i:i + n]
        energy.append(sum(map(mul, chunk, chunk)) / n)
    return silences, energy


def parse_silences(text: str, duration: float) -> list[tuple[float, float]]:
    out, start = [], None
    for m in re.finditer(r"silence_(start|end): (-?[\d.]+)", text):
        val = min(max(0.0, float(m.group(2))), duration)
        if m.group(1) == "start":
            start = val
        elif start is not None:
            out.append((start, val))
            start = None
    if start is not None and start < duration:  # lang den het file
        out.append((start, duration))
    return out


def keep_segments(silences, duration: float, pad: float, min_seg: float = 0.05) -> list[tuple[float, float]]:
    """Cac doan co tieng (bo khoang lang, chua lai `pad` giay o moi ben de khong cut cut chu)."""
    segs, cur = [], 0.0
    for s, e in sorted(silences):
        cs = 0.0 if s <= 0 else s + pad
        ce = duration if e >= duration else e - pad
        if ce - cs <= 0:
            continue
        if cs - cur >= min_seg:
            segs.append((round(cur, 3), round(cs, 3)))
        cur = max(cur, ce)
    if duration - cur >= min_seg:
        segs.append((round(cur, 3), round(duration, 3)))
    return segs


def subtract_range(segs, rng: tuple[float, float], min_seg: float = 0.05) -> list[tuple[float, float]]:
    h0, h1 = rng
    out = []
    for a, b in segs:
        for x, y in ((a, min(b, h0)), (max(a, h1), b)):
            if y - x >= min_seg:
                out.append((x, y))
    return out


def pick_hook(energy: list[float], hook_len: float, duration: float) -> tuple[float, float] | None:
    """Cua so `hook_len` giay co nang luong (RMS^2) cao nhat, khong trung voi phan mo dau."""
    w = max(1, round(hook_len / ENERGY_BLOCK))
    first = w  # bat dau tu giay thu hook_len tro di
    if len(energy) < first + w:
        return None
    cur = sum(energy[first:first + w])
    best, best_i = cur, first
    for i in range(first + 1, len(energy) - w + 1):
        cur += energy[i + w - 1] - energy[i - 1]
        if cur > best:
            best, best_i = cur, i
    if best <= 0:
        return None
    start = round(best_i * ENERGY_BLOCK, 3)
    return start, round(min(start + hook_len, duration), 3)


def build_edit(cfg: Config, duration: float, silences, energy) -> Edit | None:
    segs = [(0.0, round(duration, 3))]
    cuts, cut_seconds = 0, 0.0
    if cfg.silence_cut:
        kept = keep_segments(silences, duration, cfg.silence_pad)
        kept_len = sum(b - a for a, b in kept)
        if kept and kept_len >= 1.0 and kept != segs:  # khong cat neu gan nhu toan bo la khoang lang
            bounds = [0.0] + [x for seg in kept for x in seg] + [duration]
            cuts = sum(1 for i in range(0, len(bounds), 2) if bounds[i + 1] - bounds[i] > 1e-3)
            cut_seconds = round(duration - kept_len, 3)
            segs = kept
    hook = None
    if cfg.hook_mode in ("move", "copy"):
        hook = pick_hook(energy, cfg.hook_len, duration)
        if hook and cfg.hook_mode == "move":
            segs = subtract_range(segs, hook)
    if hook is None and segs == [(0.0, round(duration, 3))]:
        return None
    return Edit(segs, hook, cfg.hook_mode if hook else "off", cuts, cut_seconds)


def remap_cues(cues, order: list[tuple[float, float]]) -> list[tuple[float, float, str]]:
    """Doi thoi gian phu de tu video goc sang timeline sau khi cat lang/chen hook."""
    out, pos = [], 0.0
    for a, b in order:
        for s, e, text in cues:
            s2, e2 = max(s, a), min(e, b)
            if e2 - s2 > 0.05:
                out.append((pos + s2 - a, pos + e2 - a, text))
        pos += b - a
    return sorted(out)


def source_stage(info: MediaInfo, p: Params) -> tuple[list[str], str, str | None]:
    """Filter cat lang + chen hook. Tra ve (cac dong filter, nhan video, nhan audio)."""
    edit = p.edit
    if edit is None:
        return [], "0:v", "0:a:0" if info.has_audio else None
    g, segs = [], edit.segments
    # Video: giu frame trong cac doan, roi doi PTS de noi cac doan lien nhau
    sel = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in segs)
    steps, prev, pos = [], 0.0, 0.0
    for a, b in segs:
        off = a - pos  # tong thoi gian da cat truoc doan nay
        if off - prev > 1e-6:
            steps.append(f"{off - prev:.4f}*gte(T,{a:.3f})")
        prev, pos = off, pos + (b - a)
    shift = f",setpts='PTS-({'+'.join(steps)})/TB'" if steps else ""
    g.append(f"[0:v]setpts=PTS-STARTPTS,select='{sel}'{shift}[mv]")
    # Audio: atrim tung doan (chinh xac den tung mau) roi concat
    a_in = f"[0:a:0]asetpts=PTS-STARTPTS,{AFMT}"
    if len(segs) == 1:
        a, b = segs[0]
        g.append(f"{a_in},atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[ma]")
    else:
        g.append(f"{a_in},asplit={len(segs)}" + "".join(f"[ms{i}]" for i in range(len(segs))))
        for i, (a, b) in enumerate(segs):
            g.append(f"[ms{i}]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[mt{i}]")
        g.append("".join(f"[mt{i}]" for i in range(len(segs))) + f"concat=n={len(segs)}:v=0:a=1[ma]")
    if not edit.hook:
        return g, "mv", "ma"
    # Hook: input rieng (-ss/-t) de khong phai dem frame trong bo nho, dat len dau bang concat
    hook_idx = p.idx("hook")
    g.append(f"[{hook_idx}:v]setpts=PTS-STARTPTS[hv]")
    g.append(f"[{hook_idx}:a:0]asetpts=PTS-STARTPTS,{AFMT}[ha]")
    g.append("[hv][ha][mv][ma]concat=n=2:v=1:a=1[vsrc][asrc]")
    return g, "vsrc", "asrc"


# --------------------------------------------------------------------------- #
# Filter graph
# --------------------------------------------------------------------------- #
def zoom_expr(cfg: Config, p: Params) -> str:
    """z = z_mid + z_amp * sin(2*PI*t/period + phase) (+ cac nhip 'punch' cua FX)."""
    zmid = (cfg.zoom_min + cfg.zoom_max) / 2
    zamp = (cfg.zoom_max - cfg.zoom_min) / 2
    expr = f"{zmid:.5f}+{zamp:.5f}*sin(2*PI*t/{p.zoom_period}+{p.zoom_phase})"
    for t0, kind in p.fx_events:
        if kind == "punch":
            expr += f"+{PUNCH_ZOOM}*exp(-pow((t-{t0})/{PUNCH_SIGMA},2))"
    return f"({expr})"


def _enable(times: list[float], dur: float) -> str:
    return "+".join(f"between(t,{t:.3f},{t + dur:.3f})" for t in times)


def resolve_fit(cfg: Config, info: MediaInfo) -> str:
    if cfg.fit != "auto":
        return cfg.fit
    # gan 9:16 (hoac doc hon 3:4) -> cat day khung; video ngang -> nen mo
    return "crop" if info.width / info.height <= 0.75 else "blur"


def build_filter_graph(cfg: Config, p: Params, info: MediaInfo,
                       subs_file: str | None = None) -> str:
    W, H = cfg.width, cfg.height
    # Lam tron thoi luong xuat theo so frame 29.97 -> video va audio duoc ep ve cung 1 do dai
    frames, kept = output_frames(cfg, p.src_duration or info.duration)
    out_dur = kept * 1001 / 30000
    mc = MicroCut(cfg.micro_period, cfg.micro_len) if cfg.micro_cut else None

    # 0) Silence cut + hook (neu bat)
    g, v_in, a_in = source_stage(info, p)

    # 1) Speed + Drop Frame (select dat truoc fps) + co dinh 29.97 FPS
    g.append(
        f"[{v_in}]setpts=(PTS-STARTPTS)/{cfg.speed},"
        f"select='not(eq(mod(n\\,{cfg.drop_every})\\,0))',"
        f"fps={FPS}:start_time=0,"  # neo moc 0: frame 0 bi select bo se duoc bu, khong lech A/V
        # Ep video dung `frames` frame: thieu (do drop/cat doan) thi lap frame cuoi (<1 frame), du thi cat.
        # Dat truoc zoom/grain nen frame lap van chuyen dong, khong bi dung hinh.
        f"setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration=0.5,trim=end_frame={frames}"
        # Cat nhip: bo frame trong doan (1.1s, 1.2s] cua moi chu ky, noi lai lien tuc 29.97 FPS
        + (f",select='{mc.video_select()}',setpts=N*1001/30000/TB" if mc else "")
        + "[v0]"
    )

    # 2) Dua ve khung 9:16 1080x1920
    fit = resolve_fit(cfg, info)
    if p.bg_images:
        # 3 lop anh nen o duoi cung (moi anh chi giai ma 1 lan, overlay giu anh suot video)
        for i, _img in enumerate(p.bg_images):
            chain = (f"[{p.idx(f'bg{i}')}:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
                     f"crop={W}:{H},setsar=1")
            if i:
                chain += f",format=rgba,colorchannelmixer=aa={BG_LAYER_OPACITY[i]}"
            g.append(chain + f",fps={FPS}[bgl{i}]")
        base = "bgl0"
        for i in range(1, len(p.bg_images)):
            g.append(f"[{base}][bgl{i}]overlay=0:0[bgs{i}]")
            base = f"bgs{i}"
        fw, fh = even(W * cfg.bg_scale), even(H * cfg.bg_scale)
        g.append(f"[v0]scale={fw}:{fh}:force_original_aspect_ratio=decrease:"
                 f"force_divisible_by=2,setsar=1[fgv]")
        g.append(f"[{base}][fgv]overlay=(W-w)/2:(H-h)/2,setsar=1[v1]")
    elif fit == "crop":
        g.append(f"[v0]scale={W}:{H}:force_original_aspect_ratio=increase,"
                 f"crop={W}:{H},setsar=1[v1]")
    else:
        fw, fh = (W, H) if fit == "blur" else (cfg.safe_w, cfg.safe_h)
        g.append("[v0]split[bg0][fg0]")
        g.append(f"[bg0]scale={W}:{H}:force_original_aspect_ratio=increase,"
                 f"crop={W}:{H},boxblur=30:2[bg1]")
        g.append(f"[fg0]scale={fw}:{fh}:force_original_aspect_ratio=decrease:"
                 f"force_divisible_by=2[fg1]")
        g.append("[bg1][fg1]overlay=(W-w)/2:(H-h)/2,setsar=1[v1]")

    # 3) Flip + Micro-rotation + mau sac + Dynamic Zoom (scale theo frame, cat giua)
    chain = []
    if cfg.flip:
        chain.append("hflip")
    if cfg.rotate_deg:
        chain.append(f"rotate={cfg.rotate_deg}*PI/180:c=black")
    chain.append(f"eq=contrast={cfg.contrast}:brightness={cfg.brightness}:"
                 f"saturation={cfg.saturation}")
    if cfg.colorbalance:
        chain.append(f"colorbalance={cfg.colorbalance}")
    chain.append(f"hue=h={p.hue}")  # eq khong co tham so hue -> dung filter hue
    z = zoom_expr(cfg, p)
    sw, sh = f"2*trunc({W}*{z}/2)", f"2*trunc({H}*{z}/2)"
    chain.append(f"scale=w='{sw}':h='{sh}':eval=frame")
    # Tuong duong crop=W:H:(iw-ow)/2:(ih-oh)/2 nhung tinh theo kich thuoc frame hien tai
    # (bien iw cua crop chi duoc doc 1 lan luc khoi tao nen se lech tam khi zoom thay doi).
    chain.append(f"crop={W}:{H}:'({sw}-{W})/2':'({sh}-{H})/2':exact=1")
    g.append("[v1]" + ",".join(chain) + "[v2]")
    last = "v2"

    # 4) Lam mo vung hardsub/logo
    if cfg.blur_box:
        bx, by, bw, bh = cfg.blur_box
        x, y = even(W * bx), even(H * by)
        w = max(2, min(even(W * bw), W - x))
        h = max(2, min(even(H * bh), H - y))
        luma_r = max(1, min(cfg.blur_radius, min(w, h) // 2 - 1))
        chroma_r = max(1, min(cfg.blur_radius, min(w, h) // 4 - 1))
        g.append(f"[{last}]split[bm][bs]")
        g.append(f"[bs]crop={w}:{h}:{x}:{y},boxblur={luma_r}:3:{chroma_r}:3[bb]")
        g.append(f"[bm][bb]overlay={x}:{y}[v3]")
        last = "v3"

    # 5) FX chen ngau nhien (flash / glitch; punch nam trong bieu thuc zoom)
    fx = []
    flashes = [t for t, k in p.fx_events if k == "flash"]
    glitches = [t for t, k in p.fx_events if k == "glitch"]
    if flashes:
        fx.append(f"eq=brightness={FLASH_BRIGHTNESS}:enable='{_enable(flashes, FLASH_DUR)}'")
    if glitches:
        fx.append(f"chromashift=cbh={GLITCH_SHIFT}:crh=-{GLITCH_SHIFT}:"
                  f"enable='{_enable(glitches, GLITCH_DUR)}'")
    # 6) Phu de moi (nam trong safe zone)
    if subs_file:
        # subs_file luon la ten hex tuong doi ("a1b2c3d4.subs.ass"), ffmpeg chay voi cwd = thu muc
        # xuat, nen khong can escape. Khong truyen duong dan tuyet doi vao day: dau ':' (vd. "C:\")
        # bi tach thanh option khac ngay ca khi boc trong nhay don.
        fx.append(f"ass=filename={subs_file}")
    if fx:
        g.append(f"[{last}]" + ",".join(fx) + "[v4]")
        last = "v4"

    # 7) Ghost overlay
    if cfg.ghost:
        gs = cfg.ghost_size
        g.append(f"color=c=white@{cfg.ghost_alpha}:s={gs}x{gs}:r={FPS},format=rgba[ghost]")
        g.append(f"[{last}][ghost]overlay=x='(W-w)/2*(1+sin(t*{p.ghost_gx}))':"
                 f"y='(H-h)/2*(1+cos(t*{p.ghost_gy}))':shortest=1[v5]")
        last = "v5"

    # 8) Nhieu hat (grain) phu voi do mo cfg.grain
    if cfg.grain > 0:
        g.append(f"color=c=0x828282:s={W}x{H}:r={FPS},format=yuv420p,"
                 f"noise=c0s=60:c0f=t+u[grain]")
        g.append(f"[{last}]format=yuv420p[vg]")
        g.append(f"[vg][grain]blend=all_mode=overlay:all_opacity={cfg.grain}:shortest=1[v6]")
        last = "v6"

    # 9) Lop phu nga lanh 4K o TANG TREN CUNG (texture anh/video, hoac lop mau tu sinh)
    if cfg.overlay_on:
        alpha = f"format=rgba,colorchannelmixer=aa={cfg.overlay_opacity}"
        cover = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1"
        if p.overlay_file is None:
            g.append(f"color=c={cfg.overlay_color}:s={W}x{H}:r={FPS}:d=0.1,{alpha}[tex]")
        elif p.overlay_file.suffix.lower() in VIDEO_EXTS:
            g.append(f"[{p.idx('tex')}:v]{cover},fps={FPS},{alpha}[tex]")
        else:  # anh: giai ma 1 lan, overlay giu anh suot video
            g.append(f"[{p.idx('tex')}:v]{cover},{alpha},fps={FPS}[tex]")
        g.append(f"[{last}][tex]overlay=0:0:shortest=0:eof_action=repeat[v7]")
        last = "v7"
    g.append(f"[{last}]format=yuv420p[vout]")

    # ---------------- Audio: 6 buoc ----------------
    if a_in:
        g.append(
            f"[{a_in}]asetpts=PTS-STARTPTS,{AFMT},"
            # B1: speed & pitch
            f"asetrate={cfg.fast_rate},aresample={SAMPLE_RATE},"
            # B2: voice EQ
            "highpass=f=120,lowpass=f=8000,equalizer=f=1000:width_type=h:width=200:g=2,"
            # B3: compressor
            "acompressor=threshold=-15dB:ratio=4:attack=5:release=50,"
            # B4: chorus
            "chorus=0.7:0.9:15:0.4:0.25:2,"
            # B5: stereo panning
            "pan=stereo|c0=0.6*c0+0.4*c1|c1=0.4*c0+0.6*c1"
            # Cat nhip giong video: chia frame nho (32 mau) de cat chinh xac, lam mem 6ms 2 ben
            + (f",asetnsamples=n=32:p=0,volume=eval=frame:volume='{mc.declick()}',"
               f"aselect='{mc.audio_select()}',asetpts=N/SR/TB,asetnsamples=n=1024:p=0" if mc else "")
            + "[voice]"
        )
    else:
        g.append(f"anullsrc=r={SAMPLE_RATE}:cl=stereo,atrim=end={out_dur:.3f}[voice]")
    fade_st = max(0.0, out_dur - cfg.bgm_fade)
    if p.ambient:
        # B6: 3 lop am thanh moi truong (moi lop 2-3%), giong goc giu nguyen 100% (normalize=0)
        for i, (_f, vol, _start) in enumerate(p.ambient):
            g.append(f"[{p.idx(f'amb{i}')}:a:0]{AFMT},volume={vol},atrim=end={out_dur:.3f},"
                     f"asetpts=PTS-STARTPTS,afade=t=out:st={fade_st:.3f}:d={cfg.bgm_fade}[amb{i}]")
        labels = "".join(f"[amb{i}]" for i in range(len(p.ambient)))
        g.append(f"[voice]{labels}amix=inputs={1 + len(p.ambient)}:duration=first:normalize=0[vm]")
        g.append(f"[vm]apad=whole_dur={out_dur:.3f},atrim=duration={out_dur:.3f}[aout]")
        return ";\n".join(g) + "\n"

    # B6: noise layering + BGM
    g.append(f"anoisesrc=color=pink:r={SAMPLE_RATE}:a={cfg.noise_amp},"
             "aformat=sample_fmts=fltp:channel_layouts=stereo[noise]")
    g.append("[voice][noise]amix=inputs=2:duration=first:normalize=0[vn]")
    last_a = "vn"
    if p.bgm:
        g.append(
            f"[{p.idx('bgm')}:a:0]{AFMT},"
            f"volume={p.bgm_volume},atrim=end={out_dur:.3f},asetpts=PTS-STARTPTS,"
            f"afade=t=out:st={fade_st:.3f}:d={cfg.bgm_fade}[bgm]"
        )
        g.append("[vn][bgm]amix=inputs=2:duration=first:normalize=0[vm]")
        last_a = "vm"
    # Ep audio dung bang thoi luong video xuat ra (thieu thi pad im lang, du thi cat)
    g.append(f"[{last_a}]apad=whole_dur={out_dur:.3f},atrim=duration={out_dur:.3f}[aout]")
    return ";\n".join(g) + "\n"


@functools.lru_cache(maxsize=1)
def ffmpeg_major() -> int:
    """Phien ban chinh cua ffmpeg trong PATH (ban build tu git 'N-xxxxx' coi nhu ban moi nhat)."""
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-version"],
                             capture_output=True, text=True).stdout
    except OSError:
        return 0
    m = re.search(r"ffmpeg version n?(\d+)\.", out)
    return int(m.group(1)) if m else 99


def filter_script_args(filter_file: str) -> list[str]:
    # ffmpeg >= 7 doc filter tu file bang "-/filter_complex"; ffmpeg 9 da bo "-filter_complex_script",
    # con ffmpeg <= 6 chi hieu "-filter_complex_script".
    if ffmpeg_major() >= 7:
        return ["-/filter_complex", filter_file]
    return ["-filter_complex_script", filter_file]


def build_command(cfg: Config, p: Params, src: Path, filter_file: str, part_file: str) -> list[str]:
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error"]
    if cfg.threads:
        # Chay nhieu file cung luc (-j > 1): chia CPU cho ca filter graph va bo giai ma, khong chi
        # encoder. Dung -filter_complex_threads (-filter_threads chi ap dung cho -vf/-af don gian).
        cmd += ["-filter_complex_threads", str(cfg.threads), "-threads", str(cfg.threads)]
    cmd += ["-i", str(src)]
    # Input phu, dung thu tu Params.input_keys() (filter graph tham chieu theo chi so)
    for key in p.input_keys():
        if key == "hook":  # doan hook cat san bang -ss/-t
            h0, h1 = p.edit.hook
            cmd += ["-ss", f"{h0:.3f}", "-t", f"{h1 - h0:.3f}", "-i", str(src)]
        elif key == "bgm":
            cmd += ["-stream_loop", "-1", "-i", str(p.bgm.resolve())]
        elif key.startswith("amb"):  # ambient lap lai, bat dau o vi tri ngau nhien
            f, _vol, start = p.ambient[int(key[3:])]
            cmd += ["-stream_loop", "-1", "-ss", f"{start:.3f}", "-i", str(f.resolve())]
        elif key.startswith("bg"):  # anh nen: 1 frame
            cmd += ["-i", str(p.bg_images[int(key[2:])].resolve())]
        elif key == "tex":
            tex = p.overlay_file.resolve()
            cmd += (["-stream_loop", "-1"] if tex.suffix.lower() in VIDEO_EXTS else []) + ["-i", str(tex)]
    cmd += [
        *filter_script_args(filter_file),
        # Khong dung -shortest: audio/video da duoc ep cung so frame trong filter graph,
        # -shortest cua ffmpeg cat theo packet nen hay lam mat 1 frame cuoi.
        "-map", "[vout]", "-map", "[aout]",
        # Codec
        "-c:v", "libx264", "-preset", cfg.preset,
        "-profile:v", "high", "-level", "4.1", "-pix_fmt", "yuv420p",
        "-b:v", f"{p.bitrate_kbps}k",
        "-maxrate", f"{int(cfg.maxrate_mbps * 1000)}k",
        "-bufsize", f"{int(cfg.maxrate_mbps * 2000)}k",
        "-r", FPS,
        "-c:a", "aac", "-b:a", cfg.audio_bitrate, "-ar", str(SAMPLE_RATE), "-ac", "2",
    ]
    if cfg.threads:
        cmd += ["-threads", str(cfg.threads)]
    cmd += [
        # Metadata
        "-map_metadata", "-1", "-map_chapters", "-1",
        "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact",
        "-movflags", "+faststart",
        "-f", "mp4", part_file,
    ]
    return cmd


# --------------------------------------------------------------------------- #
# Xu ly 1 file
# --------------------------------------------------------------------------- #
def find_subs(src: Path, subs_arg: Path | None) -> Path | None:
    if subs_arg is not None:
        if subs_arg.is_file():
            return subs_arg
        cand = subs_arg / f"{src.stem}.srt"
        return cand if cand.is_file() else None
    cand = src.with_suffix(".srt")
    return cand if cand.is_file() else None


def process_one(src: Path, out_dir: Path, cfg: Config, seed: int, bgm_files: list[Path],
                subs_arg: Path | None, use_subs: bool, dry_run: bool, keep_temp: bool,
                progress=None) -> Result:
    """progress: ham nhan ti le 0..1 khi render (dung cho giao dien)."""
    t_start = time.monotonic()
    name = None
    try:
        info = probe(src)
        edit = None
        if cfg.silence_cut or cfg.hook_mode != "off":
            if info.has_audio:
                silences, energy = analyze_audio(src, cfg, info.duration)
                edit = build_edit(cfg, info.duration, silences, energy)
            else:
                log(f"[canh bao] {src.name}: khong co audio, bo qua silence cut / hook")
        rng = random.Random(seed)
        name = unique_name(out_dir)
        params = make_params(cfg, edit.duration if edit else info.duration, rng, name,
                             bgm_files, edit)

        subs_name = None
        cues = []
        if use_subs:
            subs_path = find_subs(src, subs_arg)
            if subs_path:
                cues = parse_srt(subs_path.read_text(encoding="utf-8", errors="replace"))
                if edit:
                    cues = remap_cues(cues, edit.output_order())
                if cues:
                    subs_name = f"{name}.subs.ass"

        graph = build_filter_graph(cfg, params, info, subs_name)
        filter_name, part_name = f"{name}.filter.txt", f"{name}.part.mp4"
        cmd = build_command(cfg, params, src.resolve(), filter_name, part_name)
        if dry_run:
            log(f"[dry-run] {src.name} -> {name}.mp4\n  {' '.join(cmd)}\n"
                f"  --- {filter_name} ---\n{graph}")
            return Result(src, True, out_dir / f"{name}.mp4", params,
                          elapsed=time.monotonic() - t_start)

        if subs_name:
            (out_dir / subs_name).write_text(build_ass(cues, cfg), encoding="utf-8")
        (out_dir / filter_name).write_text(graph, encoding="utf-8")
        on_line = None
        if progress:
            total = params.src_duration / cfg.speed
            cmd[1:1] = ["-progress", "pipe:1", "-nostats"]

            def on_line(line: str) -> None:
                key, _, val = line.strip().partition("=")
                if key in ("out_time_us", "out_time_ms") and val.isdigit() and total > 0:
                    progress(min(1.0, int(val) / 1e6 / total))
        code, _, err = run_proc(cmd, cwd=out_dir, on_line=on_line)
        if code != 0:
            tail = "\n".join(err.strip().splitlines()[-8:])
            raise RuntimeError(f"ffmpeg loi (ma {code}):\n{tail}")
        final = out_dir / f"{name}.mp4"
        (out_dir / part_name).replace(final)
        if progress:
            progress(1.0)
        return Result(src, True, final, params, elapsed=time.monotonic() - t_start)
    except Exception as exc:  # noqa: BLE001 - bao loi tung file, khong dung ca lo
        return Result(src, False, error=str(exc), elapsed=time.monotonic() - t_start)
    finally:
        if name and not dry_run and not keep_temp:
            cleanup(out_dir, name)


def describe_edit(edit: Edit | None) -> str:
    if edit is None:
        return ""
    s = f" | cat_lang={edit.cuts} ({edit.cut_seconds}s)" if edit.cuts else ""
    if edit.hook:
        s += f" | hook={edit.hook[0]}-{edit.hook[1]}s ({edit.hook_mode})"
    return s


def cleanup(out_dir: Path, name: str) -> None:
    for suffix in TEMP_SUFFIXES:
        try:
            (out_dir / f"{name}{suffix}").unlink()
        except FileNotFoundError:
            pass


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
_print_lock = threading.Lock()
_log_sink = None  # giao dien thay print bang ham rieng


def set_log_sink(fn) -> None:
    global _log_sink
    _log_sink = fn


def log(msg: str) -> None:
    if _log_sink is not None:
        _log_sink(msg)
        return
    with _print_lock:
        print(msg, flush=True)


def format_result(r: Result) -> str:
    if not r.ok:
        return f"[LOI] {r.src.name}: {r.error}"
    p = r.params
    return (f"[OK]  {r.src.name} -> {r.output.name} ({r.elapsed:.1f}s) | "
            f"bitrate={p.bitrate_kbps}k hue={p.hue} zoom_period={p.zoom_period}s "
            f"fx={len(p.fx_events)}" + (f" bgm={p.bgm.name}@{p.bgm_volume}" if p.bgm else "")
            + (f" ambient={'+'.join(f.name for f, _, _ in p.ambient)}" if p.ambient else "")
            + (f" nen={len(p.bg_images)} anh" if p.bg_images else "")
            + describe_edit(p.edit))


def collect_files(paths: list[str], exts: set[str]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            files += sorted(f for f in p.iterdir() if f.is_file() and f.suffix.lower() in exts)
        elif p.is_file():
            files.append(p)
        else:
            log(f"[canh bao] bo qua, khong tim thay: {raw}")
    return files


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Xu ly video 9:16 (1080x1920, 29.97 FPS) hang loat bang ffmpeg.")
    ap.add_argument("inputs", nargs="*",
                    help="file video hoac thu muc chua video (bo trong: dung thu muc 'input' canh script)")
    ap.add_argument("-o", "--output-dir",
                    help="thu muc xuat (mac dinh: output; khi dung thu muc 'input' canh script "
                         "thi la 'output' canh script)")
    ap.add_argument("-j", "--jobs", type=int, default=1,
                    help="so file xu ly cung luc (mac dinh: 1 = lan luot tung file)")
    ap.add_argument("--flip", action="store_true", help="lat ngang video (hflip)")
    ap.add_argument("--speed", type=float, default=1.065, help="toc do phat (mac dinh: 1.065)")
    ap.add_argument("--zoom", type=float, nargs=2, metavar=("MIN", "MAX"), default=[1.04, 1.08],
                    help="khoang dynamic zoom (mac dinh: 1.04 1.08)")
    ap.add_argument("--grain", type=float, default=0.045, help="do phu nhieu hat (mac dinh: 0.045)")
    ap.add_argument("--fx-interval", type=float, nargs=2, metavar=("MIN", "MAX"), default=[3.0, 6.0],
                    help="khoang cach chen FX, giay (mac dinh: 3 6)")
    ap.add_argument("--no-fx", action="store_true", help="tat FX")
    ap.add_argument("--rotate", type=float, default=0.7, help="goc micro-rotation, do (mac dinh: 0.7)")
    ap.add_argument("--hue-range", type=float, default=4.0, help="hue ngau nhien trong [-x, x] (mac dinh: 4)")
    ap.add_argument("--no-ghost", action="store_true", help="tat ghost overlay")
    ap.add_argument("--blur-box", type=float, nargs=4, metavar=("X", "Y", "W", "H"),
                    default=[0.0, 0.75, 1.0, 0.2], help="vung lam mo (ti le 0-1, mac dinh: 0 0.75 1 0.2)")
    ap.add_argument("--no-blur", action="store_true", help="tat lam mo vung hardsub/logo")
    ap.add_argument("--fit", choices=["auto", "crop", "blur", "safe"], default="auto",
                    help="cach dua video ve 9:16: crop=cat day khung, blur=nen mo, "
                         "safe=nen mo + dat video trong safe zone (mac dinh: auto)")
    ap.add_argument("--bgm", help="file nhac nen hoac thu muc (chon ngau nhien moi video)")
    ap.add_argument("--bgm-volume", type=float, nargs=2, metavar=("MIN", "MAX"), default=[0.03, 0.05],
                    help="am luong nhac nen (mac dinh: 0.03 0.05)")
    ap.add_argument("--subs", help="file .srt (1 video) hoac thu muc chua <ten_video>.srt; "
                                   "mac dinh tu tim <ten_video>.srt canh video")
    ap.add_argument("--no-subs", action="store_true", help="khong chen phu de")
    ap.add_argument("--bitrate", type=float, nargs=2, metavar=("MIN", "MAX"), default=[5.7, 6.3],
                    help="target bitrate ngau nhien, Mbps (mac dinh: 5.7 6.3)")
    ap.add_argument("--maxrate", type=float, default=8.0, help="maxrate, Mbps (mac dinh: 8)")
    ap.add_argument("--preset", default="medium", help="preset libx264 (mac dinh: medium)")
    ap.add_argument("--silence-cut", action="store_true",
                    help="tu dong do (silencedetect) va cat khoang lang")
    ap.add_argument("--silence-db", type=float, default=-35.0,
                    help="nguong khoang lang, dB (mac dinh: -35)")
    ap.add_argument("--silence-min", type=float, default=0.5,
                    help="khoang lang toi thieu de cat, giay (mac dinh: 0.5)")
    ap.add_argument("--silence-pad", type=float, default=0.15,
                    help="giu lai moi ben khoang lang, giay (mac dinh: 0.15)")
    ap.add_argument("--hook", choices=["off", "move", "copy"], default="off",
                    help="dua doan am thanh cao trao nhat len dau: move=chuyen (cat khoi cho cu), "
                         "copy=nhan ban (mac dinh: off)")
    ap.add_argument("--hook-len", type=float, default=3.0, help="do dai hook, giay (mac dinh: 3)")
    ap.add_argument("--micro-cut", action=argparse.BooleanOptionalAction, default=True,
                    help="cat nhip: cu moi 1.2s bo 0.1s (mac dinh: bat; tat bang --no-micro-cut)")
    ap.add_argument("--micro-period", type=float, default=1.2, help="chu ky cat nhip, giay (mac dinh: 1.2)")
    ap.add_argument("--micro-len", type=float, default=0.1, help="do dai bi cat moi chu ky, giay (mac dinh: 0.1)")
    ap.add_argument("--ambient-dir", help="thu muc am thanh moi truong: tron 3 lop (2-3%%) thay pink noise + BGM")
    ap.add_argument("--bg-dir", help="thu muc anh nen: 3 anh ngau nhien xep chong duoi video")
    ap.add_argument("--bg-scale", type=float, default=0.85,
                    help="kich thuoc video khi co anh nen, ti le khung (mac dinh: 0.85)")
    ap.add_argument("--overlay-4k", help="file texture nga lanh (anh hoac video) phu tren cung; "
                                         "bo trong = tu sinh lop mau nga lanh")
    ap.add_argument("--overlay-opacity", type=float, default=0.06, help="do mo lop phu (mac dinh: 0.06)")
    ap.add_argument("--no-overlay", action="store_true", help="tat lop phu nga lanh")
    ap.add_argument("--seed", type=int, help="seed de tai lap tham so ngau nhien")
    ap.add_argument("--dry-run", action="store_true", help="chi in lenh ffmpeg + filter, khong render")
    ap.add_argument("--keep-temp", action="store_true", help="giu lai file tam (de debug)")
    ap.add_argument("--gui", action="store_true",
                    help="mo cua so giao dien (mac dinh khi chay khong tham so)")
    return ap.parse_args(argv)


def config_from_args(a: argparse.Namespace) -> Config:
    def rng_pair(vals, label):
        lo, hi = sorted(vals)
        if lo <= 0:
            raise SystemExit(f"{label} phai > 0")
        return lo, hi

    zoom = rng_pair(a.zoom, "--zoom")
    if zoom[0] < 1.0:
        raise SystemExit("--zoom MIN phai >= 1.0")
    if a.speed <= 0:
        raise SystemExit("--speed phai > 0")
    cfg = Config(
        speed=a.speed,
        zoom_min=zoom[0], zoom_max=zoom[1],
        grain=max(0.0, a.grain),
        fx_interval=None if a.no_fx else rng_pair(a.fx_interval, "--fx-interval"),
        rotate_deg=a.rotate,
        flip=a.flip,
        hue_range=abs(a.hue_range),
        ghost=not a.no_ghost,
        blur_box=None if a.no_blur else tuple(a.blur_box),
        fit=a.fit,
        bgm_volume=tuple(sorted(a.bgm_volume)),
        bitrate_mbps=rng_pair(a.bitrate, "--bitrate"),
        maxrate_mbps=a.maxrate,
        preset=a.preset,
        silence_cut=a.silence_cut,
        silence_db=a.silence_db,
        silence_min=max(0.05, a.silence_min),
        silence_pad=max(0.0, a.silence_pad),
        hook_mode=a.hook,
        hook_len=a.hook_len,
        micro_cut=a.micro_cut,
        micro_period=a.micro_period,
        micro_len=a.micro_len,
        bg_scale=min(1.0, max(0.3, a.bg_scale)),
        overlay_on=not a.no_overlay,
        overlay_opacity=min(1.0, max(0.0, a.overlay_opacity)),
    )
    if cfg.micro_cut and not (1001 / 30000 <= cfg.micro_len < cfg.micro_period):
        raise SystemExit("--micro-len phai >= 0.034 (1 frame) va nho hon --micro-period")
    for opt, folder, exts, attr in (("--ambient-dir", a.ambient_dir, AUDIO_EXTS, "ambient_files"),
                                    ("--bg-dir", a.bg_dir, IMAGE_EXTS, "bg_files")):
        if folder:
            files = collect_files([str(Path(folder).expanduser())], exts)
            if not files:
                raise SystemExit(f"{opt}: khong tim thay file phu hop trong '{folder}'")
            setattr(cfg, attr, files)
    if a.overlay_4k:
        tex = Path(a.overlay_4k).expanduser()
        if not tex.is_file():
            raise SystemExit(f"--overlay-4k: khong tim thay file '{a.overlay_4k}'")
        cfg.overlay_file = tex
    if cfg.ambient_files and a.bgm:
        log("[canh bao] da dung --ambient-dir nen bo qua --bgm (ambient thay pink noise + BGM)")
    if cfg.hook_mode != "off" and cfg.hook_len <= 0:
        raise SystemExit("--hook-len phai > 0")
    if cfg.preset == "ultrafast":
        log("[canh bao] preset ultrafast tat CABAC/B-frame -> x264 xuat Constrained Baseline, "
            "khong phai High profile")
    # Xoay goc theta can zoom toi thieu de khong lo goc den
    th = math.radians(abs(cfg.rotate_deg))
    need = math.cos(th) + max(cfg.height / cfg.width, cfg.width / cfg.height) * math.sin(th)
    if cfg.zoom_min < need:
        log(f"[canh bao] zoom MIN {cfg.zoom_min} < {need:.4f}: co the lo goc den do xoay {cfg.rotate_deg} do")
    return cfg


SCRIPT_DIR = Path(__file__).resolve().parent


def print_usage_guide(default_in: Path) -> None:
    log(
        "Chua chon video nao de xu ly.\n\n"
        "Cach 1 - chay trong Terminal, truyen file hoac thu muc video:\n"
        "    python3 video_chuan.py <file_hoac_thu_muc_video> [-o thu_muc_xuat] [-j 4] [--flip] ...\n"
        "  Vi du:\n"
        "    python3 video_chuan.py ~/Downloads/video_goc -o ~/Downloads/video_xuat -j 4\n\n"
        "Cach 2 - bam Run (VS Code) / chay khong tham so:\n"
        f"    bo video vao thu muc: {default_in}\n"
        f"    ket qua se nam trong: {default_in.parent / 'output'}\n\n"
        "Xem tat ca tuy chon: python3 video_chuan.py --help"
    )


def main(argv: list[str] | None = None) -> int:
    a = parse_args(argv)
    if a.gui or not (sys.argv[1:] if argv is None else argv):
        rc = launch_gui()
        if rc is not None:
            return rc
    raw_inputs, out_default = a.inputs, "output"
    if not raw_inputs:
        default_in = SCRIPT_DIR / "input"
        if not (default_in.is_dir() and collect_files([str(default_in)], VIDEO_EXTS)):
            print_usage_guide(default_in)
            return 0  # chi in huong dan, khong phai loi
        raw_inputs, out_default = [str(default_in)], str(SCRIPT_DIR / "output")
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            log(f"[loi] khong tim thay {tool} trong PATH. "
                "Cai dat: macOS 'brew install ffmpeg', Windows 'winget install Gyan.FFmpeg', "
                "Ubuntu 'sudo apt install ffmpeg'")
            return 2
    cfg = config_from_args(a)
    jobs = max(1, a.jobs)
    if jobs > 1:
        cfg.threads = max(1, (os.cpu_count() or 1) // jobs)

    inputs = collect_files(raw_inputs, VIDEO_EXTS)
    if not inputs:
        log("[loi] khong co file video nao de xu ly")
        return 2
    bgm_files = collect_files([a.bgm], AUDIO_EXTS) if a.bgm else []
    if a.bgm and not bgm_files:
        log("[loi] khong tim thay file nhac nen")
        return 2
    subs_arg = Path(a.subs) if a.subs else None
    if subs_arg and subs_arg.is_file() and len(inputs) > 1:
        log("[canh bao] --subs la 1 file nhung co nhieu video: phu de se dung cho moi video")

    out_dir = Path(a.output_dir or out_default).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir = out_dir.resolve()

    master = random.Random(a.seed) if a.seed is not None else random.SystemRandom()
    seeds = [master.randrange(2**32) for _ in inputs]

    mode = "lan luot tung file" if jobs == 1 else f"{jobs} file song song"
    log(f"Xu ly {len(inputs)} file ({mode}) -> {out_dir}")
    t0 = time.monotonic()
    results: list[Result] = []

    def run(src: Path, seed: int) -> Result:
        return process_one(src, out_dir, cfg, seed, bgm_files, subs_arg,
                           not a.no_subs, a.dry_run, a.keep_temp)

    def report(r: Result) -> None:
        results.append(r)
        log(format_result(r))

    if jobs == 1:
        # Mac dinh: xu ly xong file nay moi sang file tiep theo
        for i, (src, seed) in enumerate(zip(inputs, seeds), 1):
            log(f"[{i}/{len(inputs)}] Dang xu ly: {src.name} ...")
            report(run(src, seed))
    else:
        with cf.ThreadPoolExecutor(max_workers=jobs) as pool:
            futs = [pool.submit(run, src, seed) for src, seed in zip(inputs, seeds)]
            for fut in cf.as_completed(futs):
                report(fut.result())

    ok = sum(r.ok for r in results)
    log(f"\nHoan tat: {ok}/{len(results)} thanh cong, {len(results) - ok} loi, "
        f"tong thoi gian {time.monotonic() - t0:.1f}s")
    return 0 if ok == len(results) else 1


# --------------------------------------------------------------------------- #
# Giao dien (tkinter): mo khi chay khong tham so (vd. bam Run trong VS Code) hoac voi --gui
# --------------------------------------------------------------------------- #
SETTINGS_FILE = Path.home() / ".video_chuan_gui.json"


def open_folder(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif os.name == "nt":
        os.startfile(str(path))  # noqa: S606 - chi co tren Windows
    else:
        subprocess.Popen(["xdg-open", str(path)])


def launch_gui() -> int | None:
    """Mo cua so giao dien. Tra ve None neu may khong mo duoc (de chay che do dong lenh)."""
    if tk is None:
        hint = ""
        if sys.platform == "darwin":
            hint = (f" Cai them: brew install python-tk@{sys.version_info.major}.{sys.version_info.minor}"
                    " (hoac cai Python tu python.org).")
        log("[canh bao] Python nay khong co tkinter nen khong mo duoc giao dien." + hint)
        return None
    try:
        root = tk.Tk()
    except tk.TclError as exc:  # vd. may khong co man hinh
        log(f"[canh bao] khong mo duoc giao dien: {exc}")
        return None
    GuiApp(root)
    root.mainloop()
    return 0


class GuiApp:
    POLL_MS = 100

    def __init__(self, root):
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.files: list[Path] = []
        self.running = False
        self.close_requested = False
        self.destroyed = False
        self.frac: dict[int, float] = {}
        self.names: dict[int, str] = {}
        self.current = -1
        self.total = self.done = self.ok = 0
        self.jobs = 1

        s = self._load_settings()
        self.out_var = tk.StringVar(value=s.get("output", str(SCRIPT_DIR / "output")))
        self.flip_var = tk.BooleanVar(value=s.get("flip", False))
        self.silence_var = tk.BooleanVar(value=s.get("silence_cut", False))
        self.hook_var = tk.StringVar(value=s.get("hook", "off"))
        self.subs_var = tk.BooleanVar(value=s.get("subs", True))
        self.bgm_on_var = tk.BooleanVar(value=s.get("bgm_on", False))
        self.bgm_var = tk.StringVar(value=s.get("bgm", ""))
        self.jobs_var = tk.IntVar(value=s.get("jobs", 1))
        self.fast_var = tk.BooleanVar(value=s.get("fast", False))
        self.micro_var = tk.BooleanVar(value=s.get("micro_cut", True))
        # Thu muc ambient/ va background/ canh script duoc dien san neu co
        amb_dir, bg_dir = SCRIPT_DIR / "ambient", SCRIPT_DIR / "background"
        self.ambient_on_var = tk.BooleanVar(value=s.get("ambient_on", amb_dir.is_dir()))
        self.ambient_var = tk.StringVar(value=s.get("ambient", str(amb_dir) if amb_dir.is_dir() else ""))
        self.bg_on_var = tk.BooleanVar(value=s.get("bg_on", bg_dir.is_dir()))
        self.bg_var = tk.StringVar(value=s.get("bg", str(bg_dir) if bg_dir.is_dir() else ""))
        self.overlay_on_var = tk.BooleanVar(value=s.get("overlay_on", True))
        self.overlay_var = tk.StringVar(value=s.get("overlay", ""))
        self.status_var = tk.StringVar(value="Thêm video rồi bấm Bắt đầu.")

        self._build()
        default_in = SCRIPT_DIR / "input"
        if default_in.is_dir():
            self._add_paths(collect_files([str(default_in)], VIDEO_EXTS))
        set_log_sink(lambda msg: self.q.put(("log", msg)))
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        # Dua cua so len tren cung (macOS hay mo sau VS Code)
        root.lift()
        root.attributes("-topmost", True)
        root.after(300, lambda: root.attributes("-topmost", False))
        root.after(self.POLL_MS, self._poll)
        if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
            self.start_btn.state(["disabled"])
            self.status_var.set("Chưa cài ffmpeg - xem hướng dẫn trong khung bên dưới.")
            self._append_log("Khong tim thay ffmpeg/ffprobe. Cai dat roi mo lai chuong trinh:\n"
                             "  macOS:   brew install ffmpeg\n"
                             "  Windows: winget install Gyan.FFmpeg\n"
                             "  Ubuntu:  sudo apt install ffmpeg")

    # ----- giao dien -----
    def _build(self):
        r = self.root
        r.title("Video Chuẩn")
        r.minsize(700, 800)
        bold = tkfont.nametofont("TkDefaultFont").copy()
        bold.configure(weight="bold")
        main = ttk.Frame(r, padding=14)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)

        ttk.Label(main, text="1. Video cần xử lý", font=bold).grid(row=0, column=0, sticky="w")
        lf = ttk.Frame(main)
        lf.grid(row=1, column=0, sticky="nsew", pady=4)
        lf.columnconfigure(0, weight=1)
        lf.rowconfigure(0, weight=1)
        self.listbox = tk.Listbox(lf, height=7, selectmode="extended", activestyle="none")
        sb = ttk.Scrollbar(lf, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=sb.set)
        self.listbox.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        main.rowconfigure(1, weight=1)
        bf = ttk.Frame(main)
        bf.grid(row=2, column=0, sticky="ew")
        self.edit_btns = [
            ttk.Button(bf, text="Thêm video…", command=self._pick_files),
            ttk.Button(bf, text="Thêm cả thư mục…", command=self._pick_dir),
            ttk.Button(bf, text="Bỏ video đã chọn", command=self._remove_selected),
            ttk.Button(bf, text="Xoá hết", command=self._clear),
        ]
        for i, b in enumerate(self.edit_btns):
            b.pack(side="left", padx=(0 if i == 0 else 6, 0))
        self.count_lbl = ttk.Label(bf, text="0 video")
        self.count_lbl.pack(side="right")

        ttk.Label(main, text="2. Lưu kết quả vào", font=bold).grid(row=3, column=0, sticky="w", pady=(14, 0))
        of = ttk.Frame(main)
        of.grid(row=4, column=0, sticky="ew", pady=4)
        of.columnconfigure(0, weight=1)
        ttk.Entry(of, textvariable=self.out_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(of, text="Chọn…", command=self._pick_out).grid(row=0, column=1, padx=(6, 0))
        ttk.Button(of, text="Mở thư mục", command=self._open_out).grid(row=0, column=2, padx=(6, 0))

        ttk.Label(main, text="3. Tuỳ chọn", font=bold).grid(row=5, column=0, sticky="w", pady=(14, 0))
        opt = ttk.Frame(main)
        opt.grid(row=6, column=0, sticky="ew", pady=4)
        opt.columnconfigure(1, weight=1)
        for row, (text, var) in enumerate([
            ("Lật ngang video", self.flip_var),
            ("Cắt nhịp: cứ mỗi 1.2 giây bỏ 0.1 giây (hình + tiếng)", self.micro_var),
            ("Tự cắt khoảng lặng", self.silence_var),
            ("Tự chèn phụ đề (file .srt trùng tên, đặt cạnh video)", self.subs_var),
        ]):
            ttk.Checkbutton(opt, text=text, variable=var).grid(row=row, column=0, columnspan=4, sticky="w")
        hf = ttk.Frame(opt)
        hf.grid(row=4, column=0, columnspan=4, sticky="w", pady=(4, 0))
        ttk.Label(hf, text="Hook 3 giây cao trào:").pack(side="left")
        for val, text in (("off", "Tắt"), ("move", "Chuyển lên đầu"), ("copy", "Nhân bản lên đầu")):
            ttk.Radiobutton(hf, text=text, value=val, variable=self.hook_var).pack(side="left", padx=(10, 0))
        # Hang chon duong dan: [x] nhan | o nhap | nut chon
        for row, (text, on_var, path_var, buttons) in enumerate([
            ("Âm thanh môi trường (3 lớp):", self.ambient_on_var, self.ambient_var,
             [("Thư mục…", lambda: self._pick_dir_into(self.ambient_var, self.ambient_on_var))]),
            ("Nhạc nền (khi không dùng âm thanh môi trường):", self.bgm_on_var, self.bgm_var,
             [("File…", self._pick_bgm_file), ("Thư mục…", self._pick_bgm_dir)]),
            ("Ảnh nền 3 lớp dưới video:", self.bg_on_var, self.bg_var,
             [("Thư mục…", lambda: self._pick_dir_into(self.bg_var, self.bg_on_var))]),
            ("Lớp phủ ngà lạnh trên cùng (bỏ trống = tự tạo):", self.overlay_on_var, self.overlay_var,
             [("File…", self._pick_overlay_file)]),
        ], start=5):
            ttk.Checkbutton(opt, text=text, variable=on_var).grid(row=row, column=0, sticky="w", pady=(6, 0))
            ttk.Entry(opt, textvariable=path_var).grid(row=row, column=1, sticky="ew", padx=(6, 0), pady=(6, 0))
            for col, (label, cmd) in enumerate(buttons, start=2):
                ttk.Button(opt, text=label, command=cmd).grid(row=row, column=col, padx=(6, 0), pady=(6, 0))
        jf = ttk.Frame(opt)
        jf.grid(row=9, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Label(jf, text="Số video chạy cùng lúc:").pack(side="left")
        ttk.Spinbox(jf, from_=1, to=max(1, os.cpu_count() or 1), width=4, state="readonly",
                    textvariable=self.jobs_var).pack(side="left", padx=6)
        ttk.Label(jf, text="(1 = xong video này mới sang video sau)").pack(side="left")
        ttk.Checkbutton(opt, text="Render nhanh (chất lượng giảm nhẹ)", variable=self.fast_var).grid(
            row=10, column=0, columnspan=4, sticky="w", pady=(4, 0))

        rf = ttk.Frame(main)
        rf.grid(row=7, column=0, sticky="ew", pady=(16, 4))
        self.start_btn = ttk.Button(rf, text="▶  Bắt đầu", command=self._start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(rf, text="■  Dừng", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        ttk.Label(main, textvariable=self.status_var).grid(row=8, column=0, sticky="w")
        self.pbar = ttk.Progressbar(main, maximum=1000)
        self.pbar.grid(row=9, column=0, sticky="ew", pady=4)
        tf = ttk.Frame(main)
        tf.grid(row=10, column=0, sticky="nsew")
        tf.columnconfigure(0, weight=1)
        tf.rowconfigure(0, weight=1)
        main.rowconfigure(10, weight=1)
        self.logbox = tk.Text(tf, height=8, wrap="word", state="disabled")
        lsb = ttk.Scrollbar(tf, orient="vertical", command=self.logbox.yview)
        self.logbox.configure(yscrollcommand=lsb.set)
        self.logbox.grid(row=0, column=0, sticky="nsew")
        lsb.grid(row=0, column=1, sticky="ns")

    # ----- danh sach video -----
    def _add_paths(self, paths):
        known = {p.resolve() for p in self.files}
        for p in paths:
            if p.resolve() not in known:
                known.add(p.resolve())
                self.files.append(p)
                self.listbox.insert("end", f"{p.name}    ({p.parent})")
        self.count_lbl.configure(text=f"{len(self.files)} video")

    def _pick_files(self):
        pats = " ".join(f"*{e} *{e.upper()}" for e in sorted(VIDEO_EXTS))
        names = filedialog.askopenfilenames(parent=self.root, title="Chọn video",
                                            filetypes=[("Video", pats), ("Tất cả", "*")])
        self._add_paths([Path(n) for n in names])

    def _pick_dir(self):
        d = filedialog.askdirectory(parent=self.root, title="Chọn thư mục chứa video")
        if not d:
            return
        found = collect_files([d], VIDEO_EXTS)
        if not found:
            messagebox.showinfo("Video Chuẩn", "Thư mục này không có video nào.", parent=self.root)
        self._add_paths(found)

    def _remove_selected(self):
        for i in reversed(self.listbox.curselection()):
            self.listbox.delete(i)
            del self.files[i]
        self.count_lbl.configure(text=f"{len(self.files)} video")

    def _clear(self):
        self.listbox.delete(0, "end")
        self.files.clear()
        self.count_lbl.configure(text="0 video")

    def _pick_out(self):
        d = filedialog.askdirectory(parent=self.root, title="Chọn thư mục lưu kết quả")
        if d:
            self.out_var.set(d)

    def _open_out(self):
        out = Path(self.out_var.get()).expanduser()
        out.mkdir(parents=True, exist_ok=True)
        open_folder(out)

    def _pick_bgm_file(self):
        pats = " ".join(f"*{e} *{e.upper()}" for e in sorted(AUDIO_EXTS))
        n = filedialog.askopenfilename(parent=self.root, title="Chọn nhạc nền",
                                       filetypes=[("Âm thanh", pats), ("Tất cả", "*")])
        if n:
            self.bgm_var.set(n)
            self.bgm_on_var.set(True)

    def _pick_dir_into(self, path_var, on_var):
        d = filedialog.askdirectory(parent=self.root, title="Chọn thư mục")
        if d:
            path_var.set(d)
            on_var.set(True)

    def _pick_overlay_file(self):
        pats = " ".join(f"*{e} *{e.upper()}" for e in sorted(IMAGE_EXTS | VIDEO_EXTS))
        n = filedialog.askopenfilename(parent=self.root, title="Chọn texture 4K ngà lạnh",
                                       filetypes=[("Ảnh / video", pats), ("Tất cả", "*")])
        if n:
            self.overlay_var.set(n)
            self.overlay_on_var.set(True)

    def _pick_bgm_dir(self):
        d = filedialog.askdirectory(parent=self.root, title="Chọn thư mục nhạc nền")
        if d:
            self.bgm_var.set(d)
            self.bgm_on_var.set(True)

    # ----- chay -----
    def _start(self):
        if not self.files:
            messagebox.showinfo("Video Chuẩn", "Chưa có video nào. Bấm 'Thêm video…' để chọn.",
                                parent=self.root)
            return
        bgm_files: list[Path] = []
        if self.bgm_on_var.get():
            raw = self.bgm_var.get().strip()
            bgm_files = collect_files([str(Path(raw).expanduser())], AUDIO_EXTS) if raw else []
            if not bgm_files:
                messagebox.showwarning("Video Chuẩn", "Không tìm thấy file nhạc nền. Chọn lại, "
                                       "hoặc bỏ tích 'Nhạc nền'.", parent=self.root)
                return
        out = Path(self.out_var.get().strip() or SCRIPT_DIR / "output").expanduser()
        try:
            out.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("Video Chuẩn", f"Không tạo được thư mục lưu kết quả:\n{exc}",
                                 parent=self.root)
            return
        argv = ["-o", str(out), "--hook", self.hook_var.get(),
                "--preset", "veryfast" if self.fast_var.get() else "medium"]
        if self.flip_var.get():
            argv.append("--flip")
        if self.silence_var.get():
            argv.append("--silence-cut")
        if not self.micro_var.get():
            argv.append("--no-micro-cut")
        for on_var, path_var, opt, what in ((self.ambient_on_var, self.ambient_var, "--ambient-dir",
                                             "thư mục âm thanh môi trường"),
                                            (self.bg_on_var, self.bg_var, "--bg-dir", "thư mục ảnh nền")):
            if on_var.get():
                if not path_var.get().strip():
                    messagebox.showwarning("Video Chuẩn", f"Chưa chọn {what}. Chọn thư mục, hoặc bỏ tích.",
                                           parent=self.root)
                    return
                argv += [opt, path_var.get().strip()]
        if not self.overlay_on_var.get():
            argv.append("--no-overlay")
        elif self.overlay_var.get().strip():
            argv += ["--overlay-4k", self.overlay_var.get().strip()]
        try:
            cfg = config_from_args(parse_args(argv))
        except SystemExit as exc:  # thu muc/file khong hop le
            messagebox.showwarning("Video Chuẩn", str(exc), parent=self.root)
            return
        if cfg.ambient_files:
            bgm_files = []  # am thanh moi truong thay nhac nen
        self.jobs = max(1, int(self.jobs_var.get()))
        if self.jobs > 1:
            cfg.threads = max(1, (os.cpu_count() or 1) // self.jobs)
        self._save_settings()

        files = list(self.files)
        self.total, self.done, self.ok, self.current = len(files), 0, 0, -1
        self.frac = {i: 0.0 for i in range(len(files))}
        self.names = {i: f.name for i, f in enumerate(files)}
        self.running = True
        self.start_btn.state(["disabled"])
        self.stop_btn.state(["!disabled"])
        for b in self.edit_btns:
            b.state(["disabled"])
        self.logbox.configure(state="normal")
        self.logbox.delete("1.0", "end")
        self.logbox.configure(state="disabled")
        self._append_log(f"Bat dau: {len(files)} video -> {out.resolve()}")
        cancel_event.clear()
        threading.Thread(target=self._worker, daemon=True,
                         args=(files, out.resolve(), cfg, bgm_files, self.subs_var.get())).start()

    def _worker(self, files, out_dir, cfg, bgm_files, use_subs):
        rnd = random.SystemRandom()

        def one(i: int, src: Path) -> None:
            if cancel_event.is_set():
                return
            self.q.put(("start", i))
            r = process_one(src, out_dir, cfg, rnd.randrange(2**32), bgm_files, None, use_subs,
                            False, False, progress=lambda f: self.q.put(("progress", i, f)))
            self.q.put(("done", i, r))

        try:
            if self.jobs == 1:
                for i, src in enumerate(files):
                    one(i, src)
            else:
                with cf.ThreadPoolExecutor(max_workers=self.jobs) as pool:
                    list(pool.map(lambda a: one(*a), enumerate(files)))
        except Exception as exc:  # noqa: BLE001 - khong de luong nen chet im lang
            self.q.put(("log", f"[LOI] {exc}"))
        finally:
            self.q.put(("finished",))

    def _stop(self):
        if self.running:
            cancel_all()
            self.stop_btn.state(["disabled"])
            self.status_var.set("Đang dừng…")

    def _poll(self):
        try:
            self._drain_queue()
        finally:
            if not self.destroyed:
                self.root.after(self.POLL_MS, self._poll)

    def _drain_queue(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "log":
                    self._append_log(item[1])
                elif kind == "start":
                    self.current = item[1]
                elif kind == "progress":
                    self.frac[item[1]] = item[2]
                elif kind == "done":
                    r = item[2]
                    self.frac[item[1]] = 1.0
                    self.done += 1
                    self.ok += r.ok
                    if not (cancel_event.is_set() and not r.ok):
                        self._append_log(format_result(r))
                elif kind == "finished":
                    self._finish()
                    if self.destroyed:
                        return
        except queue.Empty:
            pass
        if self.running and self.total and not cancel_event.is_set():
            overall = sum(self.frac.values()) / self.total
            self.pbar["value"] = overall * 1000
            if self.jobs == 1 and self.current >= 0:
                self.status_var.set(f"Đang xử lý {self.current + 1}/{self.total}: "
                                    f"{self.names[self.current]} - {self.frac[self.current] * 100:.0f}%")
            else:
                self.status_var.set(f"Đã xong {self.done}/{self.total} video - {overall * 100:.0f}%")

    def _finish(self):
        self.running = False
        if self.close_requested:
            self.destroyed = True
            self.root.destroy()
            return
        self.start_btn.state(["!disabled"])
        self.stop_btn.state(["disabled"])
        for b in self.edit_btns:
            b.state(["!disabled"])
        if cancel_event.is_set():
            self.status_var.set(f"Đã dừng. Xong {self.ok}/{self.total} video.")
            self._append_log(f"Da dung. Xong {self.ok}/{self.total} video.")
            return
        self.pbar["value"] = 1000
        self.status_var.set(f"Hoàn tất: {self.ok}/{self.total} video thành công.")
        self._append_log(f"Hoan tat: {self.ok}/{self.total} video thanh cong.")
        if self.ok and messagebox.askyesno(
                "Video Chuẩn", f"Xong {self.ok}/{self.total} video.\nMở thư mục kết quả?", parent=self.root):
            self._open_out()

    def _on_close(self):
        if not self.running:
            self.root.destroy()
            return
        if messagebox.askyesno("Video Chuẩn", "Đang xử lý video. Dừng lại và thoát?", parent=self.root):
            self.close_requested = True
            cancel_all()
            self.root.after(8000, self._force_close)  # phong khi luong nen khong ket thuc

    def _force_close(self):
        if not self.destroyed:
            self.destroyed = True
            self.root.destroy()

    def _append_log(self, msg: str):
        self.logbox.configure(state="normal")
        self.logbox.insert("end", msg + "\n")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    # ----- luu tuy chon cho lan sau -----
    @staticmethod
    def _load_settings() -> dict:
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_settings(self) -> None:
        data = {"output": self.out_var.get(), "flip": self.flip_var.get(),
                "silence_cut": self.silence_var.get(), "hook": self.hook_var.get(),
                "subs": self.subs_var.get(), "bgm_on": self.bgm_on_var.get(),
                "bgm": self.bgm_var.get(), "jobs": int(self.jobs_var.get()), "fast": self.fast_var.get(),
                "micro_cut": self.micro_var.get(), "ambient_on": self.ambient_on_var.get(),
                "ambient": self.ambient_var.get(), "bg_on": self.bg_on_var.get(), "bg": self.bg_var.get(),
                "overlay_on": self.overlay_on_var.get(), "overlay": self.overlay_var.get()}
        try:
            SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
