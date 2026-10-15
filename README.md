# video-chuan

Script Python (`video_chuan.py`) xử lý video 9:16 hàng loạt bằng ffmpeg: xuất 1080×1920, 29.97 FPS, H.264 High@4.1 + AAC 192k.
Chỉ dùng thư viện chuẩn của Python 3.9+, cần `ffmpeg` và `ffprobe` (có libx264, libass) trong PATH.

## Cách dùng

```bash
# Xử lý cả thư mục, 4 file song song, lật ngang, nhạc nền chọn ngẫu nhiên từ thư mục nhac/
python3 video_chuan.py input/ -o output/ -j 4 --flip --bgm nhac/

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
| `-j, --jobs` | `1` | Số file xử lý song song |
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
| `--seed` | | Cố định tham số ngẫu nhiên để tái lập |

## Pipeline

**Video:** `setpts` (speed) → `select='not(eq(mod(n\,145)\,0))'` → `fps=30000/1001` → đưa về 1080×1920 → `hflip` (tuỳ chọn)
→ `rotate=0.7*PI/180:c=black` → `eq=saturation=1.12:contrast=1.04:brightness=-0.02` + `hue=h=<-4..4>`
→ dynamic zoom `z = z_mid + z_amp·sin(2πt/period + phase)` (period 4–6 s) bằng `scale` theo từng frame + crop giữa
→ `boxblur` vùng hardsub → FX → phụ đề → ghost overlay (`color=c=white@0.015:s=150x150`, toạ độ theo `sin(t*gx)`, `cos(t*gy)`)
→ nhiễu hạt (`blend` chế độ overlay, opacity 0.045).

**FX** (chèn ngẫu nhiên mỗi 3–6 s, chọn ngẫu nhiên 1 trong 3): `flash` (sáng lên 0.12 s), `punch` (zoom giật +0.03), `glitch` (lệch kênh màu 0.15 s).

**Audio (6 bước):** `asetrate=46967,aresample=44100` → `highpass=f=120,lowpass=f=8000,equalizer=f=1000:width_type=h:width=200:g=2`
→ `acompressor=threshold=-15dB:ratio=4:attack=5:release=50` → `chorus=0.7:0.9:15:0.4:0.25:2`
→ `pan=stereo|c0=0.6*c0+0.4*c1|c1=0.4*c0+0.6*c1` → trộn `anoisesrc=color=pink:r=44100:a=0.02` + nhạc nền (âm lượng 0.03–0.05, fade-out 1.5 s cuối).

**Xuất:** `-c:v libx264 -profile:v high -level 4.1 -pix_fmt yuv420p -b:v <5700–6300k> -maxrate 8M -c:a aac -b:a 192k`
`-map_metadata -1 -fflags +bitexact -flags:v +bitexact -flags:a +bitexact`.

## Khác biệt so với bản đặc tả (và lý do)

- **Hue:** filter `eq` của ffmpeg không có tham số `hue`, nên hue được áp bằng filter `hue=h=...` ngay sau `eq`.
- **Crop giữa khi zoom:** `crop=1080:1920:(iw-ow)/2:(ih-oh)/2` sau `scale` thay đổi kích thước theo từng frame sẽ bị lệch tâm, vì `crop`
  chỉ đọc `iw/ih` một lần lúc khởi tạo. Script tính offset trực tiếp từ biểu thức zoom (giá trị bằng `(iw-ow)/2` của frame hiện tại).
- **asetrate:** 44100 × 1.065 = 46966.5 (không phải 46973), script làm tròn thành 46967 và luôn tính theo `--speed`.
  Audio được `aresample=44100` *trước* `asetrate`, nếu không file gốc 48 kHz sẽ bị chậm lại thay vì nhanh hơn.
- **29.97 FPS** dùng giá trị chuẩn `30000/1001`.
- **amix** dùng `normalize=0`, nếu không ffmpeg sẽ chia đôi âm lượng giọng nói khi trộn nhiễu.
- Video không có tiếng vẫn có track audio (nhiễu nền + nhạc nền).

## Test

```bash
python3 -m unittest discover -s tests -v
```
