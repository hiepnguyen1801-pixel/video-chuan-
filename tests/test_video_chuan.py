import json
import random
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from array import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import video_chuan as vc  # noqa: E402

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
INFO = vc.MediaInfo(duration=20.0, width=1080, height=1920, has_audio=True)


def params(cfg, seed=1, bgm=None, edit=None, info=INFO):
    dur = edit.duration if edit else info.duration
    return vc.make_params(cfg, dur, random.Random(seed), "a1b2c3d4", [Path(bgm)] if bgm else [], edit)


class TestSubtitles(unittest.TestCase):
    SRT = "﻿1\r\n00:00:01,065 --> 00:00:02,130\r\nXin <i>chao</i> {x}\r\ndong 2\r\n\r\n" \
          "2\r\n00:00:03.5 --> 00:00:04,000\r\nB\r\n"

    def test_parse_srt(self):
        cues = vc.parse_srt(self.SRT)
        self.assertEqual(len(cues), 2)
        self.assertAlmostEqual(cues[0][0], 1.065)
        self.assertAlmostEqual(cues[1][0], 3.5)
        self.assertEqual(cues[0][2], "Xin <i>chao</i> {x}\ndong 2")

    def test_ass_scaled_by_speed_and_in_safe_zone(self):
        cfg = vc.Config(micro_cut=False)
        ass = vc.build_ass(vc.parse_srt(self.SRT), cfg)
        self.assertIn("PlayResX: 1080", ass)
        self.assertIn("PlayResY: 1920", ass)
        self.assertIn(",2,100,100,250,1", ass)  # canh duoi giua, le = (1080-880)/2, (1920-1420)/2
        self.assertIn("Dialogue: 0,0:00:01.00,0:00:02.00,", ass)  # 1.065/1.065, 2.13/1.065
        self.assertIn(r"Xin {\i1}chao{\i0} (x)\Ndong 2", ass)


class TestParams(unittest.TestCase):
    def test_random_ranges(self):
        cfg = vc.Config()
        for seed in range(200):
            p = params(cfg, seed, bgm="x.mp3")
            self.assertTrue(5700 <= p.bitrate_kbps <= 6300)
            self.assertTrue(-4 <= p.hue <= 4)
            self.assertTrue(4.0 <= p.zoom_period <= 6.0)
            self.assertTrue(0.03 <= p.bgm_volume <= 0.05)

    def test_fx_interval(self):
        events = vc.fx_schedule(120.0, (3.0, 6.0), random.Random(7))
        times = [t for t, _ in events]
        self.assertTrue(3.0 <= times[0] <= 6.0)
        for a, b in zip(times, times[1:]):
            self.assertTrue(3.0 - 1e-3 <= b - a <= 6.0 + 1e-3)
        self.assertTrue(all(k in vc.FX_TYPES for _, k in events))
        self.assertEqual(vc.fx_schedule(120.0, None, random.Random(7)), [])

    def test_fast_rate(self):
        self.assertEqual(vc.Config().fast_rate, 46967)  # 44100 * 1.065 = 46966.5

    def test_unique_name(self):
        with tempfile.TemporaryDirectory() as d:
            names = {vc.unique_name(Path(d)) for _ in range(50)}
        self.assertEqual(len(names), 50)
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{8}", n) for n in names))


class TestFilterGraph(unittest.TestCase):
    def test_spec_filters_present(self):
        cfg = vc.Config(flip=True)
        p = params(cfg, bgm="bgm.mp3")
        g = vc.build_filter_graph(cfg, p, INFO, "a1b2c3d4.subs.ass")
        for s in [
            "setpts=(PTS-STARTPTS)/1.065",
            "select='not(eq(mod(n\\,145)\\,0))',fps=30000/1001",
            "tpad=stop_mode=clone:stop_duration=0.5,trim=end_frame=563",  # round(20/1.065*29.97) = round(562.8)
            "hflip,rotate=0.7*PI/180:c=black",
            "eq=contrast=1.18:brightness=-0.04:saturation=1.12,"
            "colorbalance=rs=-0.06:gs=0.01:bs=0.10:rh=-0.05:gh=0.02:bh=0.08,",
            f"hue=h={p.hue}",
            f"1.06000+0.02000*sin(2*PI*t/{p.zoom_period}+{p.zoom_phase})",
            ":eval=frame,crop=1080:1920:",
            "crop=1080:384:0:1440,boxblur",  # X=0 Y=0.75 W=1 H=0.2
            "ass=filename=a1b2c3d4.subs.ass",
            "color=c=white@0.015:s=150x150:r=30000/1001",
            f"sin(t*{p.ghost_gx})", f"cos(t*{p.ghost_gy})",
            "blend=all_mode=overlay:all_opacity=0.045",
            "asetrate=46967,aresample=44100",
            "highpass=f=120,lowpass=f=8000,equalizer=f=1000:width_type=h:width=200:g=2",
            "acompressor=threshold=-15dB:ratio=4:attack=5:release=50",
            "chorus=0.7:0.9:15:0.4:0.25:2",
            "pan=stereo|c0=0.6*c0+0.4*c1|c1=0.4*c0+0.6*c1",
            "anoisesrc=color=pink:r=44100:a=0.02",
            "[voice][noise]amix=inputs=2",
            f"volume={p.bgm_volume}",
            # cat nhip: 563 frame -> giu 518 frame = 17.284s
            "trim=end_frame=563,select='lte(mod(n*1001,36000),33000)',setpts=N*1001/30000/TB[v0]",
            "asetnsamples=n=32:p=0,volume=eval=frame:volume='st(0,floor(t*30000/36000));",
            "aselect='lte(mod(floor(t*30000/1001+1e-9)*1001,36000),33000)',asetpts=N/SR/TB",
            "afade=t=out:st=15.784:d=1.5",
            "apad=whole_dur=17.284,atrim=duration=17.284[aout]",
            "[v6][tex]overlay=0:0",  # lop phu nga lanh sau grain = tren cung
            "color=c=0xEEF1F4:s=1080x1920",
            "[vout]", "[aout]",
        ]:
            self.assertIn(s, g)

    def test_optional_stages_off(self):
        cfg = vc.Config(fx_interval=None, ghost=False, blur_box=None, grain=0)
        g = vc.build_filter_graph(cfg, params(cfg), INFO)
        for s in ["hflip", "boxblur", "white@", "blend", "chromashift", "[1:a", "ass=filename"]:
            self.assertNotIn(s, g)

    def test_no_audio_uses_silence(self):
        cfg = vc.Config()
        info = vc.MediaInfo(10.0, 1280, 720, has_audio=False)
        g = vc.build_filter_graph(cfg, params(cfg, info=info), info)
        self.assertNotIn("[0:a", g)
        self.assertIn("anullsrc", g)
        self.assertIn("[bg0]", g)  # video ngang -> fit=auto chon nen mo

    def test_command(self):
        cfg = vc.Config()
        p = params(cfg)
        cmd = " ".join(vc.build_command(cfg, p, Path("/in.mp4"), "x.filter.txt", "x.part.mp4"))
        for s in [
            "-map_metadata -1", "-fflags +bitexact", "-flags:v +bitexact", "-flags:a +bitexact",
            "-c:v libx264", "-profile:v high", "-level 4.1", "-pix_fmt yuv420p",
            "-c:a aac", "-b:a 192k", f"-b:v {p.bitrate_kbps}k", "-maxrate 8000k", "-r 30000/1001",
        ]:
            self.assertIn(s, cmd)


class TestSilenceAndHook(unittest.TestCase):
    LOG = ("[silencedetect @ 0x1] silence_start: -0.01\n"
           "[silencedetect @ 0x1] silence_end: 0.8 | silence_duration: 0.81\n"
           "[silencedetect @ 0x1] silence_start: 4\n"
           "[silencedetect @ 0x1] silence_end: 5.5 | silence_duration: 1.5\n"
           "[silencedetect @ 0x1] silence_start: 9.2\n")

    def test_parse_silences(self):
        self.assertEqual(vc.parse_silences(self.LOG, 10.0), [(0.0, 0.8), (4.0, 5.5), (9.2, 10.0)])

    def test_keep_segments(self):
        segs = vc.keep_segments(vc.parse_silences(self.LOG, 10.0), 10.0, pad=0.15)
        # dau/cuoi file cat sach phia ngoai, giua file chua lai 0.15s moi ben
        self.assertEqual(segs, [(0.0 + 0.65, 4.15), (5.35, 9.35)])

    def test_pick_hook(self):
        energy = [1.0] * 100
        energy[60:70] = [50.0] * 10   # 6.0-7.0s to nhat
        energy[0:30] = [999.0] * 30   # phan mo dau khong duoc chon
        self.assertEqual(vc.pick_hook(energy, 3.0, 10.0), (4.0, 7.0))
        self.assertIsNone(vc.pick_hook([1.0] * 50, 3.0, 5.0))  # video qua ngan

    def _edit(self, mode):
        cfg = vc.Config(silence_cut=True, hook_mode=mode)
        energy = [1.0] * 100
        energy[60:90] = [50.0] * 30  # 6.0-9.0s to nhat
        return vc.build_edit(cfg, 10.0, vc.parse_silences(self.LOG, 10.0), energy)

    def test_build_edit_move(self):
        e = self._edit("move")
        self.assertEqual(e.hook, (6.0, 9.0))
        self.assertEqual(e.segments, [(0.65, 4.15), (5.35, 6.0), (9.0, 9.35)])
        self.assertEqual(e.cuts, 3)
        self.assertAlmostEqual(e.duration, 10.0 - e.cut_seconds)  # move khong doi tong thoi luong

    def test_build_edit_copy(self):
        e = self._edit("copy")
        self.assertEqual(e.segments, [(0.65, 4.15), (5.35, 9.35)])
        self.assertAlmostEqual(e.duration, 10.0 - e.cut_seconds + 3.0)

    def test_remap_cues(self):
        cues = [(0.0, 1.0, "a"), (6.5, 7.0, "hook")]
        out = vc.remap_cues(cues, [(6.0, 9.0), (0.65, 4.15)])
        self.assertEqual([(round(s, 2), round(e, 2), t) for s, e, t in out],
                         [(0.5, 1.0, "hook"), (3.0, 3.35, "a")])

    def test_filter_and_command_with_hook(self):
        cfg = vc.Config(silence_cut=True, hook_mode="move")
        p = params(cfg, bgm="bgm.mp3", edit=self._edit("move"))
        self.assertEqual(p.input_keys(), ["hook", "bgm"])
        self.assertEqual((p.idx("hook"), p.idx("bgm")), (1, 2))
        g = vc.build_filter_graph(cfg, p, INFO)
        self.assertIn("select='between(t,0.650,4.150)+between(t,5.350,6.000)+between(t,9.000,9.350)'", g)
        self.assertIn("setpts='PTS-(0.6500*gte(T,0.650)+1.2000*gte(T,5.350)+3.0000*gte(T,9.000))/TB'", g)
        self.assertIn("asplit=3[ms0][ms1][ms2]", g)
        self.assertIn("[hv][ha][mv][ma]concat=n=2:v=1:a=1[vsrc][asrc]", g)
        self.assertIn("[vsrc]setpts=(PTS-STARTPTS)/1.065", g)
        self.assertIn("[asrc]asetpts", g)
        self.assertIn("[2:a:0]aresample", g)  # bgm la input thu 3
        cmd = " ".join(vc.build_command(cfg, p, Path("/in.mp4"), "f.txt", "o.mp4"))
        self.assertIn("-i /in.mp4 -ss 6.000 -t 3.000 -i /in.mp4 -stream_loop -1", cmd)


class TestMicroCut(unittest.TestCase):
    def test_pattern_first_cycle(self):
        mc = vc.MicroCut(1.2, 0.1)
        removed = [k for k in range(40) if not mc.keep(k)]
        self.assertEqual(removed, [33, 34, 35])  # 1.101s, 1.135s, 1.168s nam trong (1.1, 1.2]
        self.assertEqual(mc.kept_before(36), 33)
        self.assertAlmostEqual(mc.kept_before(36000) / 36000, 11 / 12, places=3)

    def test_map_time(self):
        mc = vc.MicroCut(1.2, 0.1)
        self.assertAlmostEqual(mc.map_time(0.5), 0.5, places=6)
        self.assertAlmostEqual(mc.map_time(1.15), 33 * 1001 / 30000)  # trong doan bi bo -> cho noi
        self.assertAlmostEqual(mc.map_time(1.3), 1.3 - 3 * 1001 / 30000, places=6)

    def test_audio_follows_video_frames(self):
        # Mau audio thuoc frame video nao thi giu/bo theo dung frame do
        mc = vc.MicroCut(1.2, 0.1)
        for i in range(0, 44100 * 5, 7):
            t = i / 44100
            k = int(t * 30000 / 1001 + 1e-9)
            keep_audio = (k * 1001) % mc.P <= mc.C
            self.assertEqual(keep_audio, mc.keep(k))

    def test_output_frames(self):
        self.assertEqual(vc.output_frames(vc.Config(micro_cut=False), 20.0), (563, 563))
        self.assertEqual(vc.output_frames(vc.Config(), 20.0), (563, 518))

    def test_layers_and_inputs(self):
        cfg = vc.Config(ambient_files=[Path(f"/amb/{i}.mp3") for i in range(4)],
                        bg_files=[Path(f"/bg/{i}.jpg") for i in range(5)],
                        overlay_file=Path("/tex/ivory4k.png"))
        p = params(cfg, bgm="bgm.mp3")
        self.assertIsNone(p.bgm)  # ambient thay BGM
        self.assertEqual(p.input_keys(), ["amb0", "amb1", "amb2", "bg0", "bg1", "bg2", "tex"])
        self.assertEqual(len({f for f, _, _ in p.ambient}), 3)  # 3 file khac nhau
        self.assertTrue(all(0.02 <= v <= 0.03 for _, v, _ in p.ambient))
        g = vc.build_filter_graph(cfg, p, INFO)
        self.assertIn("[voice][amb0][amb1][amb2]amix=inputs=4:duration=first:normalize=0", g)
        self.assertNotIn("anoisesrc", g)
        self.assertIn("[4:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
                      "setsar=1,fps=30000/1001[bgl0]", g)
        self.assertIn("colorchannelmixer=aa=0.5,fps=30000/1001[bgl1]", g)
        self.assertIn("colorchannelmixer=aa=0.3,fps=30000/1001[bgl2]", g)
        self.assertIn("[bgs2][fgv]overlay=(W-w)/2:(H-h)/2", g)  # video nam tren 3 lop nen
        self.assertIn("[v0]scale=918:1632:force_original_aspect_ratio=decrease", g)
        self.assertIn("[7:v]scale=1080:1920", g)  # texture la input 7
        # thu tu tang: nen -> video -> ... -> grain -> lop phu nga lanh -> xuat
        order = [g.index(x) for x in ("[bgs2][fgv]overlay", "blend=all_mode", "[v6][tex]overlay", "[vout]")]
        self.assertEqual(order, sorted(order))
        cmd = vc.build_command(cfg, p, Path("/in.mp4"), "f.txt", "o.mp4")
        i_args = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-i"]
        self.assertEqual(i_args[0], "/in.mp4")
        self.assertEqual([Path(x).parent.name for x in i_args[1:]], ["amb"] * 3 + ["bg"] * 3 + ["tex"])

    def test_cli_options(self):
        cfg = vc.config_from_args(vc.parse_args(["x.mp4", "--no-micro-cut", "--no-overlay"]))
        self.assertFalse(cfg.micro_cut)
        self.assertFalse(cfg.overlay_on)
        self.assertTrue(vc.config_from_args(vc.parse_args(["x.mp4"])).micro_cut)
        with self.assertRaises(SystemExit):
            vc.config_from_args(vc.parse_args(["x.mp4", "--micro-len", "0.01"]))
        with self.assertRaises(SystemExit):
            vc.config_from_args(vc.parse_args(["x.mp4", "--ambient-dir", "/khong/co/thu/muc"]))


@unittest.skipUnless(HAS_FFMPEG, "can ffmpeg/ffprobe")
class TestAVSyncAfterMicroCut(unittest.TestCase):
    """Chop trang va tieng bip phat cung luc o video goc phai van trung nhau sau khi xu ly."""

    def test_flash_and_beep_stay_aligned(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "amb").mkdir()
            (d / "bg").mkdir()
            for i in range(3):
                subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
                                f"anoisesrc=color=brown:a=0.3:d=4:seed={i}", str(d / "amb" / f"{i}.wav")],
                               check=True)
                subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
                                f"testsrc2=s={640 + i * 64}x480", "-frames:v", "1",
                                str(d / "bg" / f"{i}.png")], check=True)
            on = "gte(T,0.35)*lt(mod(T-0.35,0.47),0.066)"
            subprocess.run(
                ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
                 f"color=c=black:s=320x568:r=30:d=8,format=gray,geq=lum='if({on},235,16)',format=yuv420p",
                 "-f", "lavfi", "-i",
                 f"aevalsrc='if({on.replace('T', 't')},0.8*sin(2*PI*1000*t),0)':s=48000:d=8",
                 "-c:v", "libx264", "-preset", "ultrafast", "-qp", "0", str(d / "sync.mp4")], check=True)
            rc = vc.main([str(d / "sync.mp4"), "-o", str(d / "out"), "--no-fx", "--preset", "superfast",
                          "--seed", "2", "--ambient-dir", str(d / "amb"), "--bg-dir", str(d / "bg")])
            self.assertEqual(rc, 0)
            (out,) = (d / "out").iterdir()

            # Video: do sang vung giua (luon la video chinh) tung frame
            raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(out), "-vf",
                                  "crop=200:200:(iw-200)/2:(ih-200)/2,scale=4:4,format=gray",
                                  "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
            lum = [sum(raw[i:i + 16]) / 16 for i in range(0, len(raw), 16)]
            fps = 30000 / 1001
            flashes = [i / fps for i, v in enumerate(lum) if v > 128 and (i == 0 or lum[i - 1] <= 128)]

            # Audio: diem bat dau tieng bip
            pcm = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(out), "-ac", "1", "-ar", "8000",
                                  "-f", "s16le", "-"], capture_output=True, check=True).stdout
            a = array("h")
            a.frombytes(pcm)
            beeps, quiet = [], 10**9
            for i, x in enumerate(a):
                if abs(x) > 0.08 * 32767:
                    if quiet > 400:  # im lang > 50ms truoc do -> tieng bip moi
                        beeps.append(i / 8000)
                    quiet = 0
                else:
                    quiet += 1

            self.assertGreaterEqual(len(flashes), 12)
            self.assertEqual(len(flashes), len(beeps), (flashes, beeps))
            worst = max(abs(f - b) for f, b in zip(flashes, beeps))
            self.assertLess(worst, 0.05, list(zip(flashes, beeps)))  # < 1.5 frame, khong troi theo thoi gian
            dur = TestEndToEnd._durations(None, out)
            self.assertAlmostEqual(dur["audio"], dur["video"], delta=0.005)


@unittest.skipUnless(HAS_FFMPEG, "can ffmpeg/ffprobe")
class TestEndToEnd(unittest.TestCase):
    def _durations(self, path):
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration",
                              "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
        return {s["codec_type"]: float(s["duration"]) for s in json.loads(out)["streams"]}

    def test_silence_cut_hook_and_av_length(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src = d / "talk.mp4"
            # audio dai hon video 0.8s; lang 2-3.5s; to nhat 4.5-6.5s
            subprocess.run(
                ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=360x640:r=30:d=8",
                 "-f", "lavfi", "-i",
                 "aevalsrc='if(between(t,2,3.5),0,if(between(t,4.5,6.5),0.9,0.15))*sin(2*PI*300*t)'"
                 ":s=48000:d=8.8",
                 "-c:v", "libx264", "-preset", "ultrafast", str(src)], check=True)
            for mode, expect in (("move", 8 - 1.2), ("copy", 8 - 1.2 + 3)):
                out = d / mode
                rc = vc.main([str(src), "-o", str(out), "--silence-cut", "--hook", mode,
                              "--preset", "superfast", "--seed", "1"])
                self.assertEqual(rc, 0)
                (f,) = out.iterdir()
                dur = self._durations(f)
                frames = vc.output_frames(vc.Config(), expect)[1]
                self.assertAlmostEqual(dur["video"], frames * 1001 / 30000, delta=0.05)
                self.assertAlmostEqual(dur["audio"], dur["video"], delta=0.005)  # khong dung hinh cuoi clip

    def test_batch_parallel(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "in").mkdir()
            for i in range(2):
                subprocess.run(
                    ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=3",
                     "-f", "lavfi", "-i", "sine=f=300:d=3", "-shortest", "-c:v", "libx264",
                     "-preset", "ultrafast", "-metadata", "title=SECRET", str(d / "in" / f"v{i}.mp4")],
                    check=True)
            (d / "in" / "v0.srt").write_text("1\n00:00:00,500 --> 00:00:02,000\nHello\n")
            rc = vc.main([str(d / "in"), "-o", str(d / "out"), "-j", "2", "--flip",
                          "--preset", "superfast", "--seed", "3"])
            self.assertEqual(rc, 0)
            files = sorted(p.name for p in (d / "out").iterdir())
            self.assertEqual(len(files), 2, files)  # khong con file tam
            for name in files:
                self.assertRegex(name, r"^[0-9a-f]{8}\.mp4$")
                out = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries",
                     "stream=codec_name,profile,width,height,r_frame_rate,sample_rate,channels"
                     ":format=duration:format_tags", "-of", "json", str(d / "out" / name)],
                    capture_output=True, text=True, check=True).stdout
                info = json.loads(out)
                v, a = info["streams"]
                self.assertEqual((v["codec_name"], v["profile"], v["width"], v["height"]),
                                 ("h264", "High", 1080, 1920))
                self.assertEqual(v["r_frame_rate"], "30000/1001")
                self.assertEqual((a["codec_name"], a["sample_rate"], a["channels"]), ("aac", "44100", 2))
                expect = vc.output_frames(vc.Config(), 3.0)[1] * 1001 / 30000
                self.assertAlmostEqual(float(info["format"]["duration"]), expect, delta=0.05)
                self.assertNotIn("title", info["format"].get("tags", {}))


if __name__ == "__main__":
    unittest.main()
