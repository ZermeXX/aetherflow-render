import argparse
import json
import math
import os
from pathlib import Path
import shutil
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


def _make_vertical_gradient(
    width: int,
    height: int,
    top_rgb: tuple[int, int, int],
    bot_rgb: tuple[int, int, int],
    alpha: int = 255,
) -> Image.Image:
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
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "csv=p=0:s=x",
        input_video_path,
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
        "ffmpeg",
        "-y",
        "-i",
        input_video_path,
        "-framerate",
        str(fps),
        "-start_number",
        "0",
        "-i",
        png_pattern,
        "-filter_complex",
        f"[1:v]setpts=PTS-STARTPTS+{first_frame_sec}/TB[ovr];[0:v][ovr]overlay=0:0:eof_action=pass[vout]",
        "-map",
        "[vout]",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-crf",
        "18",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "copy",
        output_video_path,
    ]
    subprocess.run(cmd, check=True)
    return output_video_path


def _resolve_media_path(filename: str, assets_dir: Path) -> Path | None:
    if not filename:
        return None
    direct = assets_dir / filename
    if direct.exists():
        return direct
    fallback = Path.cwd() / filename
    if fallback.exists():
        return fallback
    return None


def _get_media_duration_sec(media_path: Path) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(media_path),
    ]
    out = subprocess.check_output(cmd, text=True).strip()
    return float(out)


def _prepare_segment(
    entry: dict,
    assets_dir: Path,
    temp_dir: Path,
    out_w: int,
    out_h: int,
    fps: int,
    lt_cfg: dict,
) -> Path:
    idx = int(entry.get("scene_index", 0))
    raw_vid_name = entry.get("video_file") or entry.get("file_name") or ""
    raw_voice_name = entry.get("voiceover_file") or entry.get("audio_file") or ""
    vid_path = _resolve_media_path(raw_vid_name, assets_dir)
    if vid_path is None:
        raise FileNotFoundError(f"Video file not found for scene {idx}: {raw_vid_name}")

    voice_path = _resolve_media_path(raw_voice_name, assets_dir)
    target_dur = float(entry.get("duration") or _get_media_duration_sec(vid_path))
    if voice_path is not None and entry.get("voiceover_file"):
        v_dur = _get_media_duration_sec(voice_path)
        pad = float(entry.get("voice_start", 0.2)) + 0.35
        target_dur = max(target_dur, v_dur + pad)

    base_clip = temp_dir / f"seg_{idx:02d}_base.mp4"
    ambient_gain_db = float(entry.get("ambient_gain_db", -18.0))
    mute_video_audio = bool(entry.get("mute_audio", False))

    vf_chain = f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2,fps={fps},setsar=1"

    if voice_path is not None and not mute_video_audio:
        filter_complex = (
            f"[0:v]{vf_chain},trim=duration={target_dur:.3f},setpts=PTS-STARTPTS[v];"
            f"[0:a]volume={ambient_gain_db}dB,apad,atrim=0:{target_dur:.3f},asetpts=PTS-STARTPTS[amb];"
            f"[1:a]volume=1.0,adelay=200|200,apad,atrim=0:{target_dur:.3f},asetpts=PTS-STARTPTS[vox];"
            f"[amb][vox]amix=inputs=2:duration=first:dropout_transition=0.2[aout]"
        )
        cmd = [
            "ffmpeg", "-y",
            "-stream_loop", "-1", "-i", str(vid_path),
            "-i", str(voice_path),
            "-filter_complex", filter_complex,
            "-map", "[v]", "-map", "[aout]",
            "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "320k", "-ar", "48000", "-ac", "2",
            str(base_clip),
        ]
    elif voice_path is not None:
        filter_complex = (
            f"[0:v]{vf_chain},trim=duration={target_dur:.3f},setpts=PTS-STARTPTS[v];"
            f"[1:a]volume=1.0,adelay=200|200,apad,atrim=0:{target_dur:.3f},asetpts=PTS-STARTPTS[aout]"
        )
        cmd = [
            "ffmpeg", "-y",
            "-stream_loop", "-1", "-i", str(vid_path),
            "-i", str(voice_path),
            "-filter_complex", filter_complex,
            "-map", "[v]", "-map", "[aout]",
            "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "320k", "-ar", "48000", "-ac", "2",
            str(base_clip),
        ]
    else:
        filter_complex = (
            f"[0:v]{vf_chain},trim=duration={target_dur:.3f},setpts=PTS-STARTPTS[v];"
            f"[0:a]volume={ambient_gain_db}dB,apad,atrim=0:{target_dur:.3f},asetpts=PTS-STARTPTS[aout]"
        )
        cmd = [
            "ffmpeg", "-y",
            "-stream_loop", "-1", "-i", str(vid_path),
            "-filter_complex", filter_complex,
            "-map", "[v]", "-map", "[aout]",
            "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "320k", "-ar", "48000", "-ac", "2",
            str(base_clip),
        ]

    subprocess.run(cmd, check=True)

    text_overlay = (entry.get("text_overlay") or "").strip()
    if lt_cfg.get("enabled", True) and text_overlay:
        show_sec = lt_cfg.get("show_sec", [0.5, 5.5])
        anim_sec = float(lt_cfg.get("anim_sec", 0.55))
        emblem_raw = lt_cfg.get("emblem_path", "aether_art_150x150.png")
        emblem_resolved = _resolve_media_path(emblem_raw, assets_dir)
        emblem_str = str(emblem_resolved) if emblem_resolved else emblem_raw

        ov_clip = temp_dir / f"seg_{idx:02d}_ov.mp4"
        apply_lower_third_to_clip(
            input_video_path=str(base_clip),
            output_video_path=str(ov_clip),
            text_overlay=text_overlay,
            fps=fps,
            emblem_path=emblem_str,
            show_start=float(show_sec[0]),
            show_end=float(show_sec[1]),
            anim_dur=anim_sec,
            work_dir=str(temp_dir / f"lt_frames_{idx:02d}"),
        )
        return ov_clip

    return base_clip


def assemble_from_manifest(manifest_path: str, assets_dir: str, output_path: str | None = None) -> Path:
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    export_cfg = manifest.get("export_settings", {})
    res_mode = str(export_cfg.get("resolution", "4k")).lower()
    if res_mode == "4k":
        out_w, out_h = 3840, 2160
    elif res_mode in ("1080p", "fhd"):
        out_w, out_h = 1920, 1080
    elif res_mode in ("720p", "hd"):
        out_w, out_h = 1280, 720
    else:
        out_w, out_h = 3840, 2160

    fps = int(export_cfg.get("fps", 24))
    lt_cfg = export_cfg.get("lower_third_config", {})
    items = manifest.get("timeline") or manifest.get("scenes") or []
    if not items:
        raise ValueError("Manifest contains no scenes or timeline entries.")

    out_name = output_path or manifest.get("project_name") or "Histoviva_EP02_Amazon_4K.mp4"
    final_out = Path(out_name)
    if final_out.suffix == "":
        final_out = final_out.with_suffix(".mp4")

    assets_root = Path(assets_dir)
    temp_dir = final_out.parent / f"_build_{final_out.stem}"
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)

    try:
        prepared_clips = []
        for entry in items:
            seg_path = _prepare_segment(
                entry=entry,
                assets_dir=assets_root,
                temp_dir=temp_dir,
                out_w=out_w,
                out_h=out_h,
                fps=fps,
                lt_cfg=lt_cfg,
            )
            prepared_clips.append(seg_path)

        concat_list = temp_dir / "concat.txt"
        with open(concat_list, "w", encoding="utf-8") as cf:
            for p in prepared_clips:
                cf.write(f"file '{p.resolve().as_posix()}'
")

        final_out.parent.mkdir(parents=True, exist_ok=True)
        concat_cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_list),
            "-c:v", "libx264", "-profile:v", "high", "-crf", str(export_cfg.get("crf", 18)),
            "-preset", str(export_cfg.get("preset", "veryfast")),
            "-pix_fmt", "yuv420p", "-r", str(fps),
            "-c:a", "aac", "-b:a", f"{export_cfg.get('audio_bitrate_kbps', 320)}k",
            "-ar", "48000", "-ac", "2",
            str(final_out),
        ]
        subprocess.run(concat_cmd, check=True)
        return final_out
    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description="Assemble Histoviva documentary from manifest.json")
    parser.add_argument("--manifest", default="manifest.json", help="Path to manifest.json")
    parser.add_argument("--assets-dir", default=".", help="Directory containing scene videos, voices, bumper, and emblem")
    parser.add_argument("--output", default=None, help="Output video path")
    args = parser.parse_args()
    result = assemble_from_manifest(args.manifest, args.assets_dir, args.output)
    print(f"Assembled video: {result}")


if __name__ == "__main__":
    main()
