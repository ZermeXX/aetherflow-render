import math
import os
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
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size=size)
    return ImageFont.load_default()


def _parse_text_overlay(raw_text: str) -> tuple[str, str]:
    if not raw_text:
        return "", ""
    parts = [p.strip() for p in raw_text.split("•") if p.strip()]
    if len(parts) >= 3:
        main_line = f"{parts[0]} • {parts[1]}".upper()
        sub_line = " • ".join(parts[2:]).upper()
        return main_line, sub_line
    if len(parts) == 2:
        return parts[0].upper(), parts[1].upper()
    return raw_text.upper(), ""


def _make_vertical_gradient(width: int, height: int, top_rgb: tuple[int, int, int], bot_rgb: tuple[int, int, int], alpha: int = 255) -> Image.Image:
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


def render_lower_third_frame(
    frame_w: int,
    frame_h: int,
    local_t: float,
    main_text: str,
    sub_text: str,
    emblem_path: str = "aether_art_150x150.png",
    show_start: float = 0.5,
    show_end: float = 5.5,
    anim_dur: float = 0.55,
) -> Image.Image:
    canvas = Image.new("RGBA", (frame_w, frame_h), (0, 0, 0, 0))
    if not main_text or local_t < show_start or local_t > show_end:
        return canvas

    scale = frame_h / 2160.0
    margin_x = int(round(120 * scale))
    margin_bottom = int(round(96 * scale))
    bar_w = frame_w - 2 * margin_x

    top_h = int(round(92 * scale))
    sub_h = int(round(52 * scale))
    total_h = top_h + sub_h
    badge_size = total_h

    base_x = margin_x
    base_y = frame_h - margin_bottom - total_h

    appear_end = show_start + anim_dur
    disappear_start = show_end - anim_dur

    if local_t < appear_end:
        p = _ease_out_cubic((local_t - show_start) / anim_dur)
        wipe_p = _ease_out_cubic(max(0.0, (local_t - (show_start + 0.12)) / max(0.05, anim_dur - 0.12)))
        badge_y_offset = int(round((1.0 - p) * 38 * scale))
        badge_alpha = int(round(255 * p))
        text_x_offset = int(round((1.0 - wipe_p) * 28 * scale))
        text_alpha = int(round(255 * wipe_p))
        sheen_pos = None
    elif local_t <= disappear_start:
        p = 1.0
        wipe_p = 1.0
        badge_y_offset = 0
        badge_alpha = 255
        text_x_offset = 0
        text_alpha = 255
        hold_elapsed = local_t - appear_end
        sheen_pos = (hold_elapsed / 1.4) if hold_elapsed <= 1.4 else None
    else:
        out_p = _ease_in_cubic((local_t - disappear_start) / anim_dur)
        p = 1.0 - out_p
        wipe_p = 1.0 - _ease_in_cubic(min(1.0, out_p * 1.15))
        badge_y_offset = int(round(out_p * 28 * scale))
        badge_alpha = int(round(255 * p))
        text_x_offset = int(round(out_p * 20 * scale))
        text_alpha = int(round(255 * max(0.0, 1.0 - out_p * 1.4)))
        sheen_pos = None

    panel_x0 = base_x + badge_size
    panel_full_w = max(1, bar_w - badge_size)
    panel_cur_w = max(0, int(round(panel_full_w * wipe_p)))

    if panel_cur_w > 2:
        panel_layer = Image.new("RGBA", (panel_full_w, total_h), (0, 0, 0, 0))
        p_draw = ImageDraw.Draw(panel_layer, "RGBA")

        p_draw.rectangle([0, 0, panel_full_w, top_h], fill=(10, 14, 18, 218))
        p_draw.rectangle([0, top_h, panel_full_w, total_h], fill=(7, 10, 14, 236))
        p_draw.rectangle([0, top_h - 1, panel_full_w, top_h + 1], fill=(255, 255, 255, 42))

        border_w = max(2, int(round(2 * scale)))
        p_draw.rectangle([0, 0, panel_full_w, border_w], fill=(245, 158, 11, 160))
        p_draw.rectangle([0, total_h - border_w, panel_full_w, total_h], fill=(153, 27, 27, 200))

        amber_w = max(8, int(round(14 * scale)))
        p_draw.rectangle([panel_full_w - amber_w, 0, panel_full_w, total_h], fill=(245, 158, 11, 235))

        if sheen_pos is not None:
            sweep_w = int(round(220 * scale))
            sweep_x = int(round(-sweep_w + sheen_pos * (panel_full_w + sweep_w * 2)))
            sheen = Image.new("RGBA", (sweep_w, total_h), (0, 0, 0, 0))
            s_draw = ImageDraw.Draw(sheen, "RGBA")
            for i in range(sweep_w):
                dist = abs(i - sweep_w / 2) / (sweep_w / 2)
                a = int(round(36 * max(0.0, 1.0 - dist) ** 2))
                s_draw.line([(i, 0), (i, total_h)], fill=(255, 255, 255, a))
            panel_layer.alpha_composite(sheen, (sweep_x, 0))

        font_main = _load_font(int(round(46 * scale)), bold=True)
        font_sub = _load_font(int(round(25 * scale)), bold=True)
        txt_layer = Image.new("RGBA", (panel_full_w, total_h), (0, 0, 0, 0))
        t_draw = ImageDraw.Draw(txt_layer, "RGBA")

        pad_x = int(round(34 * scale)) + text_x_offset
        main_y = int(round((top_h - 46 * scale) / 2)) - int(round(2 * scale))
        t_draw.text((pad_x, main_y), main_text, font=font_main, fill=(255, 255, 255, text_alpha))

        if sub_text:
            sub_y = top_h + int(round((sub_h - 25 * scale) / 2)) - int(round(2 * scale))
            t_draw.text((pad_x, sub_y), sub_text, font=font_sub, fill=(245, 158, 11, text_alpha))

        panel_layer.alpha_composite(txt_layer, (0, 0))
        cropped_panel = panel_layer.crop((0, 0, panel_cur_w, total_h))
        canvas.alpha_composite(cropped_panel, (panel_x0, base_y + badge_y_offset))

    if badge_alpha > 0:
        badge_layer = _make_vertical_gradient(
            badge_size,
            badge_size,
            top_rgb=(153, 27, 27),
            bot_rgb=(127, 29, 29),
            alpha=badge_alpha,
        )
        b_draw = ImageDraw.Draw(badge_layer, "RGBA")
        b_border = max(2, int(round(3 * scale)))
        b_draw.rectangle(
            [0, 0, badge_size - 1, badge_size - 1],
            outline=(245, 158, 11, int(round(190 * (badge_alpha / 255.0)))),
            width=b_border,
        )

        if emblem_path and os.path.exists(emblem_path):
            emblem = Image.open(emblem_path).convert("RGBA")
            ew, eh = emblem.size
            if eh > ew * 1.15:
                emblem = emblem.crop((0, 0, ew, ew))
            target_icon_sz = int(round(badge_size * 0.78))
            emblem = emblem.resize((target_icon_sz, target_icon_sz), Image.Resampling.LANCZOS)
            if badge_alpha < 255:
                r, g, b, a = emblem.split()
                a = a.point(lambda v: int(round(v * (badge_alpha / 255.0))))
                emblem = Image.merge("RGBA", (r, g, b, a))
            ix = (badge_size - target_icon_sz) // 2
            iy = (badge_size - target_icon_sz) // 2
            badge_layer.alpha_composite(emblem, (ix, iy))

        canvas.alpha_composite(badge_layer, (base_x, base_y + badge_y_offset))

    return canvas


def apply_lower_third_to_clip(
    input_video_path: str,
    output_video_path: str,
    text_overlay: str,
    fps: int = 24,
    emblem_path: str = "aether_art_150x150.png",
    show_start: float = 0.5,
    show_end: float = 5.5,
    anim_dur: float = 0.55,
    work_dir: str = "./_overlay_tmp",
) -> str:
    main_line, sub_line = _parse_text_overlay(text_overlay)
    if not main_line:
        return input_video_path

    probe_cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", input_video_path
    ]
    res_str = subprocess.check_output(probe_cmd, text=True).strip()
    w, h = map(int, res_str.split("x"))

    Path(work_dir).mkdir(parents=True, exist_ok=True)
    start_frame = max(0, int(math.floor(show_start * fps)))
    end_frame = int(math.ceil(show_end * fps))

    png_pattern = os.path.join(work_dir, "lt_%05d.png")
    for idx, f_idx in enumerate(range(start_frame, end_frame + 1)):
        t_sec = f_idx / float(fps)
        img = render_lower_third_frame(
            frame_w=w,
            frame_h=h,
            local_t=t_sec,
            main_text=main_line,
            sub_text=sub_line,
            emblem_path=emblem_path,
            show_start=show_start,
            show_end=show_end,
            anim_dur=anim_dur,
        )
        img.save(os.path.join(work_dir, f"lt_{idx:05d}.png"))

    first_frame_sec = start_frame / float(fps)
    cmd = [
        "ffmpeg", "-y",
        "-i", input_video_path,
        "-framerate", str(fps),
        "-start_number", "0",
        "-i", png_pattern,
        "-filter_complex",
        f"[1:v]setpts=PTS-STARTPTS+{first_frame_sec}/TB[ovr];[0:v][ovr]overlay=0:0:eof_action=pass[vout]",
        "-map", "[vout]",
        "-map", "0:a?",
        "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        output_video_path,
    ]
    subprocess.run(cmd, check=True)
    return output_video_path
