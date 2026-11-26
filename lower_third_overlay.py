import math
import os
import shutil
from pathlib import Path
import subprocess
from PIL import Image, ImageDraw, ImageFont


def _ease_out_cubic(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1.0 - (1.0 - t) ** 3


def _ease_in_cubic(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t ** 3


def _load_font(size: int, bold: bool = True):
    candidates = [
        "/usr/share/fonts/truetype/montserrat/Montserrat-Bold.ttf" if bold else "/usr/share/fonts/truetype/montserrat/Montserrat-SemiBold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/Montserrat-Bold.ttf" if bold else "C:/Windows/Fonts/Montserrat-SemiBold.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
    ]
    for path in (c.strip() for c in candidates):
        if os.path.exists(path):
            return ImageFont.truetype(path, size=size)
    return ImageFont.load_default()


def _parse_text_overlay(raw_text: str) -> tuple[str, str]:
    if not raw_text:
        return "", ""
    parts = [p.strip() for p in raw_text.split("•") if p.strip()]
    if len(parts) >= 3:
        return f"{parts[0]} • {parts[1]}".upper(), " • ".join(parts[2:]).upper()
    if len(parts) == 2:
        return parts[0].upper(), parts[1].upper()
    return raw_text.upper(), ""


def _make_vertical_gradient(width, height, top_rgb, bot_rgb, alpha=255):
    grad = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    px = grad.load()
    for y in range(height):
        ratio = y / max(1, height - 1)
        r = int(top_rgb[0] + (bot_rgb[0] - top_rgb[0]) * ratio)
        g = int(top_rgb[1] + (bot_rgb[1] - top_rgb[1]) * ratio)
        b = int(top_rgb[2] + (bot_rgb[2] - top_rgb[2]) * ratio)
        for x in range(width):
            px[x, y] = (r, g, b, alpha)
    return grad


def _fit_font(draw, text, size, width, bold=True):
    while size > 8:
        font = _load_font(size, bold)
        if draw.textlength(text, font=font) <= width:
            return font
        size -= 1
    return _load_font(8, bold)


def render_lower_third_frame(frame_w, frame_h, local_t, main_text, sub_text,
                             emblem_path="aether_art_150x150.png", show_start=0.5,
                             show_end=5.5, anim_dur=0.55, config=None):
    cfg = config or {}
    canvas = Image.new("RGBA", (frame_w, frame_h))
    if not main_text or not show_start <= local_t < show_end:
        return canvas
    scale = frame_h / 2160.0
    px = lambda n: max(1, round(n * scale))
    mx, mb = px(cfg.get("margin_x_4k", 120)), px(cfg.get("margin_bottom_4k", 96))
    height = px(180)
    width = min(frame_w - 2 * mx, px(2700))
    if width <= height:
        return canvas
    panel = Image.new("RGBA", (width, height))
    d = ImageDraw.Draw(panel)
    gold = (202, 166, 92, 255)
    amber = (245, 158, 11, 255)
    d.rectangle((0, 0, width-1, height-1), fill=tuple(cfg.get("bar_bg_rgba", [10,14,18,214])), outline=gold, width=px(2))
    badge = _make_vertical_gradient(height, height, (153,27,27), (91,18,29))
    panel.alpha_composite(badge)
    d.rectangle((0,0,height-1,height-1), outline=gold, width=px(3))
    if emblem_path and Path(emblem_path).is_file():
        with Image.open(emblem_path) as source:
            emblem = source.convert("RGBA")
        emblem.thumbnail((px(140),px(140)), Image.Resampling.LANCZOS)
        panel.alpha_composite(emblem, ((height-emblem.width)//2,(height-emblem.height)//2))
    x = height + px(38)
    available = width - x - px(40)
    d.line((height,px(104),width-1,px(104)), fill=(202,166,92,110), width=px(2))
    d.rectangle((width-px(8),0,width-1,height-1), fill=amber)
    main = main_text.upper()
    font = _fit_font(d,main,px(58),available)
    d.text((x,px(52)),main,font=font,fill="white",anchor="lm")
    parts = sub_text.upper().split(" • ",1)
    sub = " • ".join(parts)
    font = _fit_font(d,sub,px(36),available)
    first = parts[0]
    d.text((x,px(142)),first,font=font,fill=(235,235,235),anchor="lm")
    if len(parts)>1:
        d.text((x+d.textlength(first+" • ",font=font),px(142)),parts[1],font=font,fill=amber,anchor="lm")
        d.text((x+d.textlength(first,font=font),px(142))," • ",font=font,fill=gold,anchor="lm")
    anim = min(max(0.0,anim_dur),(show_end-show_start)/2)
    opacity = min(_ease_out_cubic((local_t-show_start)/anim),_ease_out_cubic((show_end-local_t)/anim)) if anim else 1.0
    panel.putalpha(panel.getchannel("A").point(lambda v: round(v*opacity)))
    canvas.alpha_composite(panel,(mx,frame_h-mb-height+round(px(30)*(1-opacity))))
    return canvas


def apply_lower_third_to_clip(input_video_path, output_video_path, text_overlay, fps=24,
                              emblem_path="aether_art_150x150.png", show_start=0.5,
                              show_end=5.5, anim_dur=0.55, work_dir="./_overlay_tmp", config=None, crf=18, preset="veryfast", maxrate=0, bufsize=0):
    main_line, sub_line = _parse_text_overlay(text_overlay)
    if not main_line:
        return input_video_path
    probe_cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", input_video_path]
    res_str = subprocess.check_output(probe_cmd, text=True).strip()
    w, h = map(int, res_str.split("x"))
    Path(work_dir).mkdir(parents=True, exist_ok=True)
    start_frame = max(0, int(math.floor(show_start * fps)))
    end_frame = int(math.ceil(show_end * fps))
    png_pattern = os.path.join(work_dir, "lt_%05d.png")
    for idx, f_idx in enumerate(range(start_frame, end_frame + 1)):
        t_sec = f_idx / float(fps)
        img = render_lower_third_frame(w, h, t_sec, main_line, sub_line, emblem_path,
                                       show_start, show_end, anim_dur, config)
        img.save(os.path.join(work_dir, f"lt_{idx:05d}.png"))
    first_frame_sec = start_frame / float(fps)
    cmd = [
        "ffmpeg", "-y", "-i", input_video_path,
        "-framerate", str(fps), "-start_number", "0", "-i", png_pattern,
        "-filter_complex",
        f"[1:v]setpts=PTS-STARTPTS+{first_frame_sec}/TB[ovr];[0:v][ovr]overlay=0:0:eof_action=pass[vout]",
        "-map", "[vout]", "-map", "0:a?",
        "-c:v", "libx264", "-crf", str(crf), "-preset", preset, "-pix_fmt", "yuv420p",
        "-c:a", "copy", output_video_path,
    ]
    if maxrate > 0:
        cmd[-1:-1] = ["-maxrate", f"{maxrate}M", "-bufsize", f"{bufsize}M"]
    subprocess.run(cmd, check=True)
    shutil.rmtree(work_dir, ignore_errors=True)
    return output_video_path
