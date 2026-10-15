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
| `--zoom MIN MAX` | `1.04 1.08` | Khoảng dynamic zoom |
| `--grain` | `0.045` | Độ phủ nhiễu hạt |
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
| `--ambient-dir` | | Thư mục âm thanh môi trường: trộn 3 lớp ngẫu nhiên (mỗi lớp 2–3%), thay cho pink noise và nhạc nền |
| `--bg-dir` / `--bg-scale` | — / `0.85` | Thư mục ảnh nền: 3 ảnh ngẫu nhiên xếp chồng dưới video (độ mờ 100% / 50% / 30%), video thu còn 85% khung |
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
→ cắt nhịp `select='lte(mod(n*1001,36000),33000)',setpts=N*1001/30000/TB` → 3 lớp ảnh nền (nếu có) → `rotate=0.7*PI/180:c=black`
→ `eq=contrast=1.18:brightness=-0.04:saturation=1.12` → `colorbalance=rs=-0.06:gs=0.01:bs=0.10:rh=-0.05:gh=0.02:bh=0.08` + `hue=h=<-4..4>`
→ dynamic zoom `z = z_mid + z_amp·sin(2πt/period + phase)` (period 4–6 s) bằng `scale` theo từng frame + crop giữa
→ `boxblur` vùng hardsub → FX → phụ đề → ghost overlay (`color=c=white@0.015:s=150x150`, toạ độ theo `sin(t*gx)`, `cos(t*gy)`)
→ nhiễu hạt (`blend` chế độ overlay, opacity 0.045).

**FX** (chèn ngẫu nhiên mỗi 3–6 s, chọn ngẫu nhiên 1 trong 3): `flash` (sáng lên 0.12 s), `punch` (zoom giật +0.03), `glitch` (lệch kênh màu 0.15 s).

**Audio (6 bước):** `asetrate=46967,aresample=44100` → `highpass=f=120,lowpass=f=8000,equalizer=f=1000:width_type=h:width=200:g=2`
→ `acompressor=threshold=-15dB:ratio=4:attack=5:release=50` → `chorus=0.7:0.9:15:0.4:0.25:2`
→ `pan=stereo|c0=0.6*c0+0.4*c1|c1=0.4*c0+0.6*c1` → cắt nhịp (`aselect` + `asetpts=N/SR/TB`, làm mềm 6 ms mỗi điểm cắt)
→ trộn 3 lớp ambient `amix=inputs=4:duration=first:normalize=0` (giọng gốc giữ 100%).
Nếu không có `--ambient-dir`: trộn `anoisesrc=color=pink:r=44100:a=0.02` + nhạc nền (âm lượng 0.03–0.05, fade-out 1.5 s cuối).

**Lớp phủ ngà lạnh** nằm ở tầng trên cùng, sau grain. Thứ tự các lớp từ dưới lên: 3 ảnh nền → video → mờ hardsub → FX
→ phụ đề → ghost → grain → lớp phủ ngà lạnh.

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
