#!/usr/bin/env python3
"""video_chuan.py - xu ly video 9:16 hang loat bang ffmpeg.

Vi du:
    python3 video_chuan.py input/ -o output/ -j 4 --flip --bgm nhac/

Yeu cau: ffmpeg + ffprobe (ban co libx264, libass) trong PATH. Chi dung thu vien chuan Python.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import math
import os
import random
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".flv", ".ts", ".wmv"}
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus"}

FPS = "30000/1001"  # 29.97 FPS (gia tri NTSC chuan)
SAMPLE_RATE = 44100
TEMP_SUFFIXES = (".filter.txt", ".subs.ass", ".part.mp4")

# Hieu ung FX chen theo chu ky ngau nhien
FX_TYPES = ("flash", "punch", "glitch")
FLASH_DUR, FLASH_BRIGHTNESS = 0.12, 0.07
GLITCH_DUR, GLITCH_SHIFT = 0.15, 6
PUNCH_ZOOM, PUNCH_SIGMA = 0.03, 0.08


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
    contrast: float = 1.04
    brightness: float = -0.02
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

    @property
    def fast_rate(self) -> int:
        """Sample rate cho asetrate: 44100 * speed."""
        return int(SAMPLE_RATE * self.speed + 0.5)


@dataclass
class MediaInfo:
    duration: float
    width: int
    height: int
    has_audio: bool


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


def probe(path: Path) -> MediaInfo:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries",
        "format=duration:stream=codec_type,width,height,duration"
        ":stream_tags=rotate:stream_side_data=rotation",
        "-of", "json", str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe loi: {proc.stderr.strip()}")
    data = json.loads(proc.stdout or "{}")
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

    duration = data.get("format", {}).get("duration") or video.get("duration")
    if not duration or float(duration) <= 0:
        raise RuntimeError("khong doc duoc thoi luong video")
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    return MediaInfo(float(duration), width, height, has_audio)


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
    for start, end, body in cues:
        lines.append(
            f"Dialogue: 0,{_ass_time(start / cfg.speed)},{_ass_time(end / cfg.speed)},"
            f"Default,,0,0,0,,{_ass_text(body)}"
        )
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


def make_params(cfg: Config, info: MediaInfo, rng: random.Random, name: str,
                bgm_files: list[Path]) -> Params:
    out_duration = info.duration / cfg.speed
    return Params(
        name=name,
        hue=round(rng.uniform(-cfg.hue_range, cfg.hue_range), 2),
        zoom_period=round(rng.uniform(*cfg.zoom_period), 3),
        zoom_phase=round(rng.uniform(0, 2 * math.pi), 3),
        ghost_gx=round(rng.uniform(0.3, 0.9), 3),
        ghost_gy=round(rng.uniform(0.3, 0.9), 3),
        bitrate_kbps=int(round(rng.uniform(*cfg.bitrate_mbps) * 1000)),
        fx_events=fx_schedule(out_duration, cfg.fx_interval, rng),
        bgm=rng.choice(bgm_files) if bgm_files else None,
        bgm_volume=round(rng.uniform(*cfg.bgm_volume), 4) if bgm_files else 0.0,
    )


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
    out_dur = info.duration / cfg.speed
    g: list[str] = []

    # 1) Speed + Drop Frame (select dat truoc fps) + co dinh 29.97 FPS
    g.append(
        f"[0:v]setpts=(PTS-STARTPTS)/{cfg.speed},"
        f"select='not(eq(mod(n\\,{cfg.drop_every})\\,0))',"
        f"fps={FPS}[v0]"
    )

    # 2) Dua ve khung 9:16 1080x1920
    fit = resolve_fit(cfg, info)
    if fit == "crop":
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
    chain.append(f"eq=saturation={cfg.saturation}:contrast={cfg.contrast}:"
                 f"brightness={cfg.brightness}")
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
    g.append(f"[{last}]format=yuv420p[vout]")

    # ---------------- Audio: 6 buoc ----------------
    if info.has_audio:
        g.append(
            "[0:a:0]asetpts=PTS-STARTPTS,"
            f"aresample={SAMPLE_RATE},aformat=sample_fmts=fltp:channel_layouts=stereo,"
            # B1: speed & pitch
            f"asetrate={cfg.fast_rate},aresample={SAMPLE_RATE},"
            # B2: voice EQ
            "highpass=f=120,lowpass=f=8000,equalizer=f=1000:width_type=h:width=200:g=2,"
            # B3: compressor
            "acompressor=threshold=-15dB:ratio=4:attack=5:release=50,"
            # B4: chorus
            "chorus=0.7:0.9:15:0.4:0.25:2,"
            # B5: stereo panning
            "pan=stereo|c0=0.6*c0+0.4*c1|c1=0.4*c0+0.6*c1[voice]"
        )
    else:
        g.append(f"anullsrc=r={SAMPLE_RATE}:cl=stereo,atrim=end={out_dur:.3f}[voice]")
    # B6: noise layering + BGM
    g.append(f"anoisesrc=color=pink:r={SAMPLE_RATE}:a={cfg.noise_amp},"
             "aformat=sample_fmts=fltp:channel_layouts=stereo[noise]")
    g.append("[voice][noise]amix=inputs=2:duration=first:normalize=0[vn]")
    last_a = "vn"
    if p.bgm:
        fade_st = max(0.0, out_dur - cfg.bgm_fade)
        g.append(
            f"[1:a:0]aresample={SAMPLE_RATE},aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"volume={p.bgm_volume},atrim=end={out_dur:.3f},asetpts=PTS-STARTPTS,"
            f"afade=t=out:st={fade_st:.3f}:d={cfg.bgm_fade}[bgm]"
        )
        g.append("[vn][bgm]amix=inputs=2:duration=first:normalize=0[vm]")
        last_a = "vm"
    g.append(f"[{last_a}]apad=whole_dur={out_dur:.3f},atrim=end={out_dur:.3f}[aout]")
    return ";\n".join(g) + "\n"


def build_command(cfg: Config, p: Params, src: Path, filter_file: str, part_file: str) -> list[str]:
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
           "-i", str(src)]
    if p.bgm:
        cmd += ["-stream_loop", "-1", "-i", str(p.bgm.resolve())]
    cmd += [
        "-filter_complex_script", filter_file,
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
                subs_arg: Path | None, use_subs: bool, dry_run: bool, keep_temp: bool) -> Result:
    t_start = time.monotonic()
    name = None
    try:
        info = probe(src)
        rng = random.Random(seed)
        name = unique_name(out_dir)
        params = make_params(cfg, info, rng, name, bgm_files)

        subs_name = None
        cues = []
        if use_subs:
            subs_path = find_subs(src, subs_arg)
            if subs_path:
                cues = parse_srt(subs_path.read_text(encoding="utf-8", errors="replace"))
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
        proc = subprocess.run(cmd, cwd=out_dir, capture_output=True, text=True)
        if proc.returncode != 0:
            tail = "\n".join(proc.stderr.strip().splitlines()[-8:])
            raise RuntimeError(f"ffmpeg loi (ma {proc.returncode}):\n{tail}")
        final = out_dir / f"{name}.mp4"
        (out_dir / part_name).replace(final)
        return Result(src, True, final, params, elapsed=time.monotonic() - t_start)
    except Exception as exc:  # noqa: BLE001 - bao loi tung file, khong dung ca lo
        return Result(src, False, error=str(exc), elapsed=time.monotonic() - t_start)
    finally:
        if name and not dry_run and not keep_temp:
            cleanup(out_dir, name)


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


def log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


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
    ap.add_argument("inputs", nargs="+", help="file video hoac thu muc chua video")
    ap.add_argument("-o", "--output-dir", default="output", help="thu muc xuat (mac dinh: output)")
    ap.add_argument("-j", "--jobs", type=int, default=1, help="so file xu ly song song (mac dinh: 1)")
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
    ap.add_argument("--seed", type=int, help="seed de tai lap tham so ngau nhien")
    ap.add_argument("--dry-run", action="store_true", help="chi in lenh ffmpeg + filter, khong render")
    ap.add_argument("--keep-temp", action="store_true", help="giu lai file tam (de debug)")
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
    )
    if cfg.preset == "ultrafast":
        log("[canh bao] preset ultrafast tat CABAC/B-frame -> x264 xuat Constrained Baseline, "
            "khong phai High profile")
    # Xoay goc theta can zoom toi thieu de khong lo goc den
    th = math.radians(abs(cfg.rotate_deg))
    need = math.cos(th) + max(cfg.height / cfg.width, cfg.width / cfg.height) * math.sin(th)
    if cfg.zoom_min < need:
        log(f"[canh bao] zoom MIN {cfg.zoom_min} < {need:.4f}: co the lo goc den do xoay {cfg.rotate_deg} do")
    return cfg


def main(argv: list[str] | None = None) -> int:
    a = parse_args(argv)
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            log(f"[loi] khong tim thay {tool} trong PATH")
            return 2
    cfg = config_from_args(a)
    jobs = max(1, a.jobs)
    if jobs > 1:
        cfg.threads = max(1, (os.cpu_count() or 1) // jobs)

    inputs = collect_files(a.inputs, VIDEO_EXTS)
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

    out_dir = Path(a.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir = out_dir.resolve()

    master = random.Random(a.seed) if a.seed is not None else random.SystemRandom()
    seeds = [master.randrange(2**32) for _ in inputs]

    log(f"Xu ly {len(inputs)} file, {jobs} tien trinh song song -> {out_dir}")
    t0 = time.monotonic()
    results: list[Result] = []
    with cf.ThreadPoolExecutor(max_workers=jobs) as pool:
        futs = {
            pool.submit(process_one, src, out_dir, cfg, seed, bgm_files, subs_arg,
                        not a.no_subs, a.dry_run, a.keep_temp): src
            for src, seed in zip(inputs, seeds)
        }
        for fut in cf.as_completed(futs):
            r = fut.result()
            results.append(r)
            if r.ok:
                p = r.params
                log(f"[OK]  {r.src.name} -> {r.output.name} ({r.elapsed:.1f}s) | "
                    f"bitrate={p.bitrate_kbps}k hue={p.hue} zoom_period={p.zoom_period}s "
                    f"fx={len(p.fx_events)}" + (f" bgm={p.bgm.name}@{p.bgm_volume}" if p.bgm else ""))
            else:
                log(f"[LOI] {r.src.name}: {r.error}")

    ok = sum(r.ok for r in results)
    log(f"\nHoan tat: {ok}/{len(results)} thanh cong, {len(results) - ok} loi, "
        f"tong thoi gian {time.monotonic() - t0:.1f}s")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
