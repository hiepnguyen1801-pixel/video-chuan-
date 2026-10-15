# video-chuan

Script Python (`video_chuan.py`) xử lý video 9:16 hàng loạt bằng ffmpeg: xuất 1080×1920, 29.97 FPS, H.264 High@4.1 + AAC 192k.
Chỉ dùng thư viện chuẩn của Python 3.9+, cần `ffmpeg` và `ffprobe` (có libx264, libass) trong PATH.

## Cài đặt

- macOS: `brew install ffmpeg` · Windows: `winget install Gyan.FFmpeg` · Ubuntu: `sudo apt install ffmpeg`
- Đã kiểm tra với ffmpeg 6.1, 7.0, 8.1 và 9.0. Từ ffmpeg 7 trở lên, script dùng `-/filter_complex <file>`
  (ffmpeg 9 đã bỏ `-filter_complex_script`), với ffmpeg 6 trở xuống thì dùng `-filter_complex_script`.

## Cách dùng

**Cách nhanh nhất (bấm Run trong VS Code hoặc chạy không tham số):** tạo thư mục `input` cạnh file `video_chuan.py`
và bỏ video vào đó. Kết quả sẽ nằm trong thư mục `output` cạnh script. Nếu chưa có thư mục `input`, script chỉ in hướng dẫn.
Nếu cạnh script có thư mục `hook/` (clip mở đầu) hoặc `bgvideo/` (video nền), giao diện tự điền sẵn và bật
các tuỳ chọn tương ứng.

**Chạy trong Terminal:**

```bash
# Xử lý cả thư mục (lần lượt từng video), lật ngang, nhạc nền chọn ngẫu nhiên từ thư mục nhac/
python3 video_chuan.py input/ -o output/ --flip --bgm nhac/

# Xem lệnh ffmpeg + filter graph mà không render
python3 video_chuan.py clip.mp4 --dry-run
```

File xuất có tên là chuỗi hex ngẫu nhiên 8 ký tự (vd. `a1b2c3d4.mp4`). Các file tạm `<hex>.filter.txt`, `<hex>.subs.ass`, `<hex>.part.mp4`
được xoá sau mỗi file, kể cả khi render lỗi (`--keep-temp` để giữ lại khi debug).

Phụ đề: nếu cạnh video có `<tên_video>.srt` (hoặc chỉ định bằng `--subs`), phụ đề sẽ được chuyển sang ASS, chia thời gian theo tốc độ phát
và đặt trong safe zone 880×1420 (lề trái/phải 100 px, lề dưới 250 px). `--no-subs` để tắt.

## Tham số

| Tham số | Mặc định | Ý nghĩa |
|---|---|---|
| `-o, --output-dir` | `output` | Thư mục xuất |
| `-j, --jobs` | `1` | Số video xử lý cùng lúc (mặc định `1` = xong video này mới sang video tiếp theo) |
| `--flip` | tắt | Lật ngang (`hflip`) |
| `--speed` | `1.065` | Tốc độ phát (video `setpts`, audio `asetrate`) |
| `--zoom-level` | `1.23` | Zoom gốc của video chính; camera trôi sin/cos trong phần dư ra (zoom "thở" ±0.02) |
| `--zoom MIN MAX` | zoom-level ±0.02 | Tự chọn khoảng zoom |
| `--shake` | `3` | Rung nhẹ mỗi frame (px), `0` = tắt |
| `--grain` | `3` | Hạt nhiễu ở tầng cuối `noise=alls=3:allf=t+u`, `0` = tắt |
| `--fx-interval MIN MAX` / `--no-fx` | `3 6` | Khoảng cách (giây) giữa các FX |
| `--rotate` | `0.7` | Góc micro-rotation (độ) |
| `--hue-range` | `4` | Hue ngẫu nhiên trong `[-x, x]` độ |
| `--blur-box X Y W H` / `--no-blur` | `0 0.75 1 0.2` | Vùng làm mờ hardsub/logo (tỉ lệ khung hình) |
| `--no-ghost` | | Tắt ghost overlay |
| `--fit` | `auto` | `crop` cắt đầy khung, `blur` nền mờ, `safe` nền mờ + video trong safe zone; `auto` chọn `crop` cho video dọc, `blur` cho video ngang |
| `--bgm` | | File nhạc nền hoặc thư mục |
| `--bgm-volume MIN MAX` | `0.03 0.05` | Âm lượng nhạc nền |
| `--subs` / `--no-subs` | tự tìm `.srt` | Phụ đề |
| `--bitrate MIN MAX` | `5.7 6.3` | Target bitrate ngẫu nhiên (Mbps) |
| `--maxrate` | `8` | Maxrate (Mbps), bufsize = 2× maxrate |
| `--preset` | `medium` | Preset libx264 (`ultrafast` sẽ ra Baseline, không phải High) |
| `--silence-cut` | tắt | Tự dò (`silencedetect`) và cắt khoảng lặng |
| `--silence-db` / `--silence-min` / `--silence-pad` | `-35` / `0.5` / `0.15` | Ngưỡng lặng (dB), độ dài lặng tối thiểu để cắt (s), phần giữ lại mỗi bên (s) |
| `--hook {off,move,copy}` | `off` | Đưa đoạn âm thanh cao trào nhất lên đầu: `move` chuyển hẳn (tổng thời lượng không đổi), `copy` nhân bản (dài thêm đúng độ dài hook) |
| `--hook-len` | `3` | Độ dài hook (giây) |
| `--micro-cut` / `--no-micro-cut` | bật | Cắt nhịp: cứ mỗi `--micro-period` (1.2 s) bỏ `--micro-len` (0.1 s), cả hình lẫn tiếng |
| `--bg-video-dir` / `--bg-audio-vol` / `--bg-scale` | — / `0.03` / `0.85` | Thư mục video nền đặt dưới video chính, tiếng trộn 3%, video chính thu còn 85% khung |
| `--bg-segment` | `2` | Video nền ghép từ các đoạn N giây, cả hình lẫn tiếng; mỗi đoạn lấy từ video nền khác đoạn trước. `0` = 1 video chạy liền |
| `--hook-dir` / `--intro-trim` | — / `3` | Thư mục clip mở đầu (2–3 s): chọn ngẫu nhiên 1 clip, bỏ 3 s đầu của video gốc và ghép clip vào đầu |
| `--overlay-4k` / `--overlay-opacity` / `--no-overlay` | tự tạo / `0.06` | Lớp phủ ngà lạnh trên cùng: file texture (ảnh hoặc video), bỏ trống thì tự tạo lớp màu `#EEF1F4` |
| `--seed` | | Cố định tham số ngẫu nhiên để tái lập |

## Pipeline

**Silence Cut + Energy Hook** (bật bằng `--silence-cut`, `--hook move|copy`): đọc audio 1 lần, vừa chạy
`silencedetect=noise=-35dB:d=0.5` vừa lấy PCM để đo năng lượng theo từng khối 0.1 s. Khoảng lặng bị cắt (chừa 0.15 s mỗi bên),
cửa sổ 3 s có năng lượng cao nhất (không tính 3 s mở đầu) được đưa lên đầu video. Video cắt bằng `select` + `setpts`,
audio cắt bằng `atrim` + `concat` (chính xác tới từng mẫu), hook lấy từ input thứ 2 (`-ss/-t`) rồi ghép bằng `concat`.
Phụ đề `.srt` được dời thời gian theo đúng các đoạn cắt/hook.

**Đồng bộ hình/tiếng:** thời lượng xuất được làm tròn theo số frame 29.97. Video được ép đúng số frame đó
(`tpad` lặp frame cuối nếu thiếu, `trim=end_frame` nếu thừa, đặt trước zoom/grain nên không bị đứng hình),
audio được `apad` + `atrim=duration` về cùng độ dài. Thời lượng gốc lấy theo luồng video, nên nếu audio gốc dài hơn hình
thì phần thừa bị cắt bỏ. Không dùng `-shortest` vì ffmpeg cắt theo packet và hay làm mất frame cuối.

**Video:** `setpts` (speed) → `select='not(eq(mod(n\,145)\,0))'` → `fps=30000/1001` → đưa về 1080×1920 → `hflip` (tuỳ chọn)
→ cắt nhịp `select='lte(mod(n*1001,36000),33000)',setpts=N*1001/30000/TB` → `rotate=0.7*PI/180:c=black`
→ zoom gốc 1.23 (±0.02 theo sin, chu kỳ 4–6 s) bằng `scale` theo từng frame → `crop` với toạ độ X/Y = giữa + trôi theo `sin`/`cos`
(chu kỳ 5–9 s, 60% phần dư ra) + rung nhẹ 3 px mỗi frame, luôn kẹp trong phần dư nên không lộ viền
→ `boxblur` vùng hardsub của video chính → (nếu có) thu video chính còn 85% và đặt lên video nền
→ `eq=contrast=1.15:brightness=-0.03:saturation=1.08` → `colorbalance=rs=-0.05:gs=0.01:bs=0.08:rh=-0.04:gh=0.02:bh=0.06` + `hue=h=<-4..4>`
→ FX → phụ đề → ghost overlay (`color=c=white@0.015:s=150x150`, toạ độ theo `sin(t*gx)`, `cos(t*gy)`) → lớp phủ ngà lạnh
→ hạt nhiễu `noise=alls=3:allf=t+u` ở tầng cuối cùng.

**Video nền chia đoạn** (`--bg-video-dir`, `--bg-segment 2`): mỗi video mở tối đa 3 video nền, mỗi video chạy liền
từ một vị trí ngẫu nhiên (lặp lại nếu ngắn). Mỗi đoạn 2 s (60 frame 29.97) chọn một nguồn khác đoạn trước:
hình dùng `fps` + `select` theo số thứ tự frame rồi `interleave`, nên điểm cắt rơi đúng ranh giới frame;
tiếng của mỗi nguồn chỉ bật trong các đoạn của nguồn đó (cùng ranh giới với hình, khối 10 ms). Thư mục chỉ có
1 video thì dùng chính video đó ở 2 vị trí khác nhau. Vì số video mở cùng lúc cố định, RAM không tăng theo độ dài
video (đo được khoảng 280 MB cho 80 s video nền). File hỏng hoặc không có hình trong thư mục được bỏ qua kèm cảnh báo.

**Clip mở đầu** (`--hook-dir`): bỏ 3 s đầu video gốc, đưa clip mở đầu và video gốc về cùng khung 1080×1920 rồi nối bằng
`concat`. Cả hai cùng đi qua tăng tốc, cắt nhịp, chỉnh màu và 6 bước xử lý âm thanh. Phụ đề `.srt` được dời theo độ dài clip mở đầu.

**FX** (chèn ngẫu nhiên mỗi 3–6 s, chọn ngẫu nhiên 1 trong 3): `flash` (sáng lên 0.12 s), `punch` (zoom giật +0.03), `glitch` (lệch kênh màu 0.15 s).

**Audio (6 bước):** `asetrate=46967,aresample=44100` → `highpass=f=120,lowpass=f=8000,equalizer=f=1000:width_type=h:width=200:g=2`
→ `acompressor=threshold=-15dB:ratio=4:attack=5:release=50` → `chorus=0.7:0.9:15:0.4:0.25:2`
→ `pan=stereo|c0=0.6*c0+0.4*c1|c1=0.4*c0+0.6*c1` → cắt nhịp (`aselect` + `asetpts=N/SR/TB`, làm mềm 6 ms mỗi điểm cắt)
→ trộn các lớp nền trong một `amix=...:duration=first:normalize=0` (giọng gốc giữ 100%): `anoisesrc=color=pink:r=44100:a=0.02`,
nhạc nền 0.03–0.05 (nếu có `--bgm`) và tiếng video nền 3% (nếu có). Các lớp nền fade-out 1.5 s cuối.

Thứ tự các lớp từ dưới lên: video nền (nếu có) → video chính (đã zoom, camera, mờ hardsub) → FX → phụ đề → ghost
→ lớp phủ ngà lạnh → hạt nhiễu.

**Xuất:** `-c:v libx264 -profile:v high -level 4.1 -pix_fmt yuv420p -b:v <5700–6300k> -maxrate 8M -c:a aac -b:a 192k`
`-map_metadata -1 -fflags +bitexact -flags:v +bitexact -flags:a +bitexact`.

## Khác biệt so với bản đặc tả (và lý do)

- **Hue:** filter `eq` của ffmpeg không có tham số `hue`, nên hue được áp bằng filter `hue=h=...` ngay sau `eq`.
- **Crop giữa khi zoom:** `crop=1080:1920:(iw-ow)/2:(ih-oh)/2` sau `scale` thay đổi kích thước theo từng frame sẽ bị lệch tâm, vì `crop`
  chỉ đọc `iw/ih` một lần lúc khởi tạo. Script tính offset trực tiếp từ biểu thức zoom (giá trị bằng `(iw-ow)/2` của frame hiện tại).
- **asetrate:** 44100 × 1.065 = 46966.5 (không phải 46973), script làm tròn thành 46967 và luôn tính theo `--speed`.
  Audio được `aresample=44100` *trước* `asetrate`, nếu không file gốc 48 kHz sẽ bị chậm lại thay vì nhanh hơn.
- **29.97 FPS** dùng giá trị chuẩn `30000/1001`.
- **Drop frame:** `select='not(eq(mod(n\,145)\,0))'` bỏ cả frame số 0. Vì vậy `fps` được đặt `start_time=0`,
  frame đầu được bù bằng cách lặp lại, tránh để hình chạy sớm hơn tiếng 1 frame.
- **amix** dùng `normalize=0`, nếu không ffmpeg sẽ chia đôi âm lượng giọng nói khi trộn nhiễu.
- Video không có tiếng vẫn có track audio (nhiễu nền + nhạc nền).

## Cắt nhịp và đồng bộ hình/tiếng

Cắt nhịp chạy trên timeline 29.97 FPS sau khi đã tăng tốc. Frame `k` (bắt đầu ở `k*1001/30000` giây) bị bỏ khi
`(k*1001) mod 36000 > 33000`, tức là đúng điều kiện `mod(t,1.2) > 1.1` nhưng tính bằng số nguyên để không sai số làm tròn.
Audio dùng chính công thức đó cho từng nhóm 32 mẫu, nên mỗi mẫu âm thanh được giữ hoặc bỏ theo đúng frame hình chứa nó.
Hai bên mỗi điểm cắt được làm mềm 6 ms để không có tiếng "tách". Khác với lệnh mẫu trong bản yêu cầu:
`select` dùng số frame `n` thay cho `t`, và `setpts=N*1001/30000/TB` thay cho `N/(29.97*TB)`, vì 29.97 chỉ là số làm tròn của 30000/1001.

Kiểm tra: video thử có 16 lần chớp trắng kèm tiếng bíp cùng lúc. Sau khi xử lý, cả 16 cặp vẫn còn đủ,
lệch lớn nhất 26 ms (chưa tới 1 frame) và không tăng dần theo thời gian.

Lưu ý: tham số `rs/gs/bs` của `colorbalance` là vùng tối (shadows), `rh/gh/bh` là vùng sáng (highlights);
nếu muốn chỉnh vùng trung tính (midtones) thì dùng `rm/gm/bm`.

## Test

```bash
python3 -m unittest discover -s tests -v
```
