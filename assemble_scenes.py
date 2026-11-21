import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

BATCH_SIZE = 6
BREATH_PAUSE_SEC = 0.35  # Пауза дыхания диктора после фразы (сек)

VALID_XFADE_TRANSITIONS = {
    "fade", "wipeleft", "wiperight", "wipeup", "wipedown",
    "slideleft", "slideright", "slideup", "slidedown",
    "circlecrop", "rectcrop", "distance", "fadeblack", "fadewhite",
    "radial", "smoothleft", "smoothright", "smoothup", "smoothdown",
    "circleopen", "circleclose", "vertopen", "vertclose",
    "horzopen", "horzclose", "dissolve", "pixelize",
    "diagtl", "diagtr", "diagbl", "diagbr",
    "hlslice", "hrslice", "vuslice", "vdslice",
    "hblur", "fadegrays", "wipetl", "wipetr", "wipebl", "wipebr",
    "squeezeh", "squeezev", "zoomin",
}


def run_cmd(cmd: list[str]) -> None:
    print("▶", " ".join(cmd))
    subprocess.run(cmd, check=True)


def get_media_duration(media_path: Path) -> float:
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(media_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    try:
        val = float(res.stdout.strip())
        return val if val > 0.05 else 0.0
    except ValueError:
        return 0.0


def has_audio_stream(media_path: Path) -> bool:
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=index",
        "-of", "csv=p=0",
        str(media_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    return bool(res.stdout.strip())


def resolve_asset(filename: str | None, work_dir: Path, folder_id: str | None) -> Path | None:
    if not filename:
        return None

    base_name = Path(filename).name
    stem = Path(base_name).stem
    ext = Path(base_name).suffix
    name_variants = [base_name]
    m = re.match(r"^(.*?)(\d+)([^\d]*)$", stem)
    if m:
        prefix, digits, suffix = m.groups()
        num = int(digits)
        for pad in (2, 3, 1):
            cand = f"{prefix}{num:0{pad}d}{suffix}{ext}"
            if cand not in name_variants:
                name_variants.append(cand)

    for variant in name_variants:
        for path in (
            work_dir / variant,
            Path("assets") / variant,
            Path("input") / variant,
            Path(variant),
        ):
            if path.is_file():
                return path

    if folder_id:
        remote = os.getenv("GDRIVE_REMOTE", "gdrive")
        for variant in name_variants:
            dest = work_dir / variant
            dest.parent.mkdir(parents=True, exist_ok=True)
            res = subprocess.run(
                [
                    "rclone", "copyto",
                    f"{remote}:{variant}",
                    str(dest),
                    "--drive-root-folder-id", folder_id,
                ],
                capture_output=True,
                text=True,
            )
            if res.returncode == 0 and dest.is_file():
                print(f"📥 Скачан файл: {variant}")
                return dest

    return None


def parse_overlay_config(manifest: dict) -> tuple[set[str], str]:
    """Возвращает (набор активных оверлеев, выбранный стиль плашки lower_thirds)."""
    ui_montage = (manifest.get("studio_ui_presets") or {}).get("tab_1_montage") or {}
    raw_overlays = manifest.get("overlays")
    if raw_overlays is None:
        raw_overlays = ui_montage.get("overlays") or []

    enabled: set[str] = set()
    raw_style = str(
        manifest.get("lower_third_style")
        or ui_montage.get("lower_third_style")
        or "rossiya24"
    ).strip().lower()

    def register_key(raw_key: str) -> None:
        k = raw_key.strip().lower()
        if any(w in k for w in ("grain", "35mm", "film", "vignette", "зерн", "пленк", "плёнк")):
            enabled.add("film_grain")
        if any(w in k for w in ("glow", "halation", "bloom", "warm", "свечен")):
            enabled.add("warm_glow")
        if any(w in k for w in ("particle", "snow", "spark", "dust", "частиц", "искр", "снег")):
            enabled.add("particles")
        if any(w in k for w in ("lower", "third", "date", "location", "титр", "дат", "локац")):
            enabled.add("lower_thirds")

    if isinstance(raw_overlays, dict):
        for k, v in raw_overlays.items():
            if bool(v):
                register_key(str(k))
    elif isinstance(raw_overlays, list):
        for item in raw_overlays:
            if isinstance(item, str):
                register_key(item)
            elif isinstance(item, dict):
                if item.get("enabled", True):
                    ident = item.get("id") or item.get("type") or item.get("name") or ""
                    register_key(str(ident))
                    if item.get("style"):
                        raw_style = str(item["style"]).strip().lower()

    if "ictv" in raw_style or "факт" in raw_style:
        lt_style = "ictv"
    elif "sky" in raw_style:
        lt_style = "sky_news"
    else:
        lt_style = "rossiya24"

    return enabled, lt_style


def escape_drawtext(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\u2019")
        .replace("%", "\\%")
        .replace(",", "\\,")
    )


def build_lower_third_filters(
    hist_date: str,
    hist_loc: str,
    style: str,
    width: int,
    height: int,
    lt_start: float,
    lt_end: float,
) -> list[str]:
    """Строит фильтры FFmpeg для новостных плашек: rossiya24, ictv, sky_news."""
    font_bold = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    font_reg = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    scale_k = width / 3840.0
    max_bar_w = width - int(round(280 * scale_k))

    alpha_expr = (
        f"if(lt(t-{lt_start:.3f}\\,0.35)\\,(t-{lt_start:.3f})/0.35\\,"
        f"if(gt(t\\,{lt_end:.3f}-0.35)\\,({lt_end:.3f}-t)/0.35\\,1))"
    )
    enable_expr = f"between(t,{lt_start:.3f},{lt_end:.3f})"

    date_up = hist_date.upper()
    loc_up = hist_loc.upper()
    filters: list[str] = []

    if style == "ictv":
        box_x = int(round(140 * scale_k))
        main_y = height - int(round(250 * scale_k))
        main_h = int(round(104 * scale_k))
        accent_w = max(6, int(round(16 * scale_k)))
        text_x = box_x + accent_w + int(round(32 * scale_k))

        main_text = loc_up if loc_up else date_up
        main_w = min(max_bar_w, max(int(round(620 * scale_k)), int(round((len(main_text) * 26 + 110) * scale_k))))

        if date_up and loc_up:
            tag_h = int(round(52 * scale_k))
            tag_y = main_y - tag_h
            tag_w = min(main_w, max(int(round(260 * scale_k)), int(round((len(date_up) * 21 + 64) * scale_k))))
            filters.append(
                f"drawbox=x={box_x}:y={tag_y}:w={tag_w}:h={tag_h}:color=0xd97706@0.96:t=fill:enable='{enable_expr}'"
            )
            filters.append(
                f"drawtext=fontfile={font_bold}:text='{escape_drawtext(date_up)}':"
                f"fontcolor=white:fontsize={max(16, int(round(30 * scale_k)))}:"
                f"x={box_x + int(round(28 * scale_k))}:y={tag_y + int(round(11 * scale_k))}:"
                f"alpha='{alpha_expr}':enable='{enable_expr}'"
            )

        filters.append(
            f"drawbox=x={box_x}:y={main_y}:w={main_w}:h={main_h}:color=0x0f172a@0.90:t=fill:enable='{enable_expr}'"
        )
        filters.append(
            f"drawbox=x={box_x}:y={main_y}:w={accent_w}:h={main_h}:color=0xf59e0b@0.98:t=fill:enable='{enable_expr}'"
        )
        filters.append(
            f"drawtext=fontfile={font_bold}:text='{escape_drawtext(main_text)}':"
            f"fontcolor=white:fontsize={max(20, int(round(44 * scale_k)))}:"
            f"x={text_x}:y={main_y + int(round(30 * scale_k))}:"
            f"alpha='{alpha_expr}':enable='{enable_expr}'"
        )

    elif style == "sky_news":
        box_x = int(round(140 * scale_k))
        box_y = height - int(round(265 * scale_k))
        box_h = int(round(122 * scale_k))
        red_w = int(round(112 * scale_k))
        text_x = box_x + red_w + int(round(34 * scale_k))

        top_line = date_up if date_up else loc_up
        sub_line = hist_loc if (date_up and hist_loc) else ""
        longest = max(len(top_line) * 25, len(sub_line) * 19)
        total_w = min(max_bar_w, max(int(round(680 * scale_k)), red_w + int(round((longest + 90) * scale_k))))

        filters.append(
            f"drawbox=x={box_x}:y={box_y}:w={total_w}:h={box_h}:color=0x1e293b@0.82:t=fill:enable='{enable_expr}'"
        )
        filters.append(
            f"drawbox=x={box_x}:y={box_y}:w={red_w}:h={box_h}:color=0xe11d48@0.95:t=fill:enable='{enable_expr}'"
        )
        filters.append(
            f"drawbox=x={box_x + red_w}:y={box_y}:w={total_w - red_w}:h={max(2, int(round(4 * scale_k)))}:"
            f"color=white@0.35:t=fill:enable='{enable_expr}'"
        )

        if sub_line:
            filters.append(
                f"drawtext=fontfile={font_bold}:text='{escape_drawtext(top_line)}':"
                f"fontcolor=white:fontsize={max(18, int(round(42 * scale_k)))}:"
                f"x={text_x}:y={box_y + int(round(22 * scale_k))}:"
                f"alpha='{alpha_expr}':enable='{enable_expr}'"
            )
            filters.append(
                f"drawtext=fontfile={font_reg}:text='{escape_drawtext(sub_line)}':"
                f"fontcolor=0xe2e8f0:fontsize={max(15, int(round(32 * scale_k)))}:"
                f"x={text_x}:y={box_y + int(round(72 * scale_k))}:"
                f"alpha='{alpha_expr}':enable='{enable_expr}'"
            )
        else:
            filters.append(
                f"drawtext=fontfile={font_bold}:text='{escape_drawtext(top_line)}':"
                f"fontcolor=white:fontsize={max(20, int(round(44 * scale_k)))}:"
                f"x={text_x}:y={box_y + int(round(38 * scale_k))}:"
                f"alpha='{alpha_expr}':enable='{enable_expr}'"
            )

    else:
        box_x = int(round(140 * scale_k))
        box_y = height - int(round(255 * scale_k))
        box_h = int(round(108 * scale_k))
        stripe_h = max(4, int(round(8 * scale_k)))

        if date_up and loc_up:
            red_w = max(int(round(260 * scale_k)), int(round((len(date_up) * 25 + 68) * scale_k)))
            blue_w = max(int(round(520 * scale_k)), int(round((len(loc_up) * 26 + 84) * scale_k)))
            if red_w + blue_w > max_bar_w:
                blue_w = max(int(round(360 * scale_k)), max_bar_w - red_w)

            filters.append(
                f"drawbox=x={box_x}:y={box_y}:w={red_w}:h={box_h}:color=0xdc2626@0.95:t=fill:enable='{enable_expr}'"
            )
            filters.append(
                f"drawbox=x={box_x + red_w}:y={box_y}:w={blue_w}:h={box_h}:color=0x1e3a8a@0.92:t=fill:enable='{enable_expr}'"
            )
            filters.append(
                f"drawbox=x={box_x + red_w}:y={box_y + box_h - stripe_h}:w={blue_w}:h={stripe_h}:"
                f"color=0x3b82f6@0.95:t=fill:enable='{enable_expr}'"
            )
            filters.append(
                f"drawtext=fontfile={font_bold}:text='{escape_drawtext(date_up)}':"
                f"fontcolor=white:fontsize={max(18, int(round(40 * scale_k)))}:"
                f"x={box_x + int(round(32 * scale_k))}:y={box_y + int(round(34 * scale_k))}:"
                f"alpha='{alpha_expr}':enable='{enable_expr}'"
            )
            filters.append(
                f"drawtext=fontfile={font_bold}:text='{escape_drawtext(loc_up)}':"
                f"fontcolor=white:fontsize={max(18, int(round(42 * scale_k)))}:"
                f"x={box_x + red_w + int(round(36 * scale_k))}:y={box_y + int(round(33 * scale_k))}:"
                f"alpha='{alpha_expr}':enable='{enable_expr}'"
            )
        else:
            single_text = date_up if date_up else loc_up
            red_w = max(8, int(round(22 * scale_k)))
            blue_w = min(max_bar_w - red_w, max(int(round(520 * scale_k)), int(round((len(single_text) * 26 + 84) * scale_k))))
            filters.append(
                f"drawbox=x={box_x}:y={box_y}:w={red_w}:h={box_h}:color=0xdc2626@0.95:t=fill:enable='{enable_expr}'"
            )
            filters.append(
                f"drawbox=x={box_x + red_w}:y={box_y}:w={blue_w}:h={box_h}:color=0x1e3a8a@0.92:t=fill:enable='{enable_expr}'"
            )
            filters.append(
                f"drawbox=x={box_x + red_w}:y={box_y + box_h - stripe_h}:w={blue_w}:h={stripe_h}:"
                f"color=0x3b82f6@0.95:t=fill:enable='{enable_expr}'"
            )
            filters.append(
                f"drawtext=fontfile={font_bold}:text='{escape_drawtext(single_text)}':"
                f"fontcolor=white:fontsize={max(18, int(round(42 * scale_k)))}:"
                f"x={box_x + red_w + int(round(34 * scale_k))}:y={box_y + int(round(33 * scale_k))}:"
                f"alpha='{alpha_expr}':enable='{enable_expr}'"
            )

    return filters


def get_normalized_scenes(manifest: dict) -> list[dict]:
    raw_scenes = (
        manifest.get("timeline")
        or manifest.get("scenes")
        or manifest.get("timeline_slots")
        or []
    )
    normalized = []

    for item in raw_scenes:
        scene = dict(item)
        assets = scene.get("assets") or {}
        vid_proc = scene.get("video_processing") or {}
        aud_proc = scene.get("audio_processing") or {}
        trans_in = vid_proc.get("transition_in") or {}
        trans_out = vid_proc.get("transition_out") or {}

        video_file = (
            scene.get("file_name")
            or scene.get("video_file")
            or assets.get("video_file")
            or assets.get("image_reference")
        )
        voice_file = (
            scene.get("voiceover_file")
            or scene.get("audio_file")
            or assets.get("voiceover_file")
        )

        scene["file_name"] = video_file
        scene["video_file"] = video_file
        if voice_file:
            scene["voiceover_file"] = voice_file
            scene["audio_file"] = voice_file

        if "transition" not in scene and trans_in:
            scene["transition"] = trans_in.get("type", "cut")
            scene["transition_duration"] = float(trans_in.get("duration_sec", 0.0))

        if "transition_out_type" not in scene and trans_out:
            scene["transition_out_type"] = trans_out.get("type", "none")
            scene["transition_out_duration"] = float(trans_out.get("duration_sec", 0.0))

        if "mute_audio" not in scene and "video_ambient_mute" in aud_proc:
            scene["mute_audio"] = bool(aud_proc["video_ambient_mute"])
        if "ambient_gain_db" not in scene and "video_ambient_gain_db" in aud_proc:
            scene["ambient_gain_db"] = float(aud_proc["video_ambient_gain_db"])
        if "voiceover_gain_db" not in scene and "voiceover_gain_db" in aud_proc:
            scene["voiceover_gain_db"] = float(aud_proc["voiceover_gain_db"])

        normalized.append(scene)

    return normalized


def prepare_scene(
    scene: dict,
    idx: int,
    width: int,
    height: int,
    fps: int,
    folder_id: str | None,
    overlays: set[str],
    lt_style: str,
) -> tuple[Path, float, float, float]:
    assets = scene.get("assets") or {}
    video_name = (
        scene.get("file_name")
        or scene.get("video_file")
        or assets.get("video_file")
        or assets.get("image_reference")
    )
    if not video_name:
        raise KeyError(f"В сцене #{idx + 1} не указано имя видеофайла")

    voice_name = (
        scene.get("voiceover_file")
        or scene.get("audio_file")
        or assets.get("voiceover_file")
    )

    trans_type = str(scene.get("transition") or "cut").strip().lower()
    cf_dur = float(scene.get("transition_duration") or 0.0)
    if trans_type == "cut" or cf_dur <= 0.0:
        cf_dur = 0.0

    cf_frames = int(round(cf_dur * fps)) if (idx > 0 and cf_dur > 0.0) else 0
    cf_dur = cf_frames / float(fps)
    lead_in_ms = int(round(cf_dur * 1000))

    work_dir = Path("build/scenes")
    work_dir.mkdir(parents=True, exist_ok=True)

    src_video = resolve_asset(video_name, work_dir, folder_id)
    if not src_video or not src_video.is_file():
        raise FileNotFoundError(f"Не найден видеофайл для сцены #{idx + 1}: {video_name}")

    src_voice = resolve_asset(voice_name, work_dir, folder_id) if voice_name else None
    is_image = src_video.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}

    manifest_dur = float(scene.get("duration_sec") or scene.get("duration") or 8.0)
    real_voice_dur = get_media_duration(src_voice) if (src_voice and src_voice.is_file()) else 0.0

    if real_voice_dur > 0.0:
        raw_target_dur = cf_dur + real_voice_dur + BREATH_PAUSE_SEC
    else:
        raw_target_dur = max(manifest_dur, cf_dur + 1.0)

    exact_frames = max(fps, int(round(raw_target_dur * fps)))
    target_dur = exact_frames / float(fps)

    real_video_dur = target_dur if is_image else get_media_duration(src_video)
    if real_video_dur <= 0.05:
        real_video_dur = target_dur

    out_path = work_dir / f"norm_{idx + 1:03d}.mkv"

    cmd = ["ffmpeg", "-y"]
    speed_factor = 1.0

    if is_image:
        cmd += ["-loop", "1", "-t", f"{target_dur:.6f}", "-i", str(src_video)]
        v_filters = [
            "setpts=PTS-STARTPTS",
            f"scale={width}:{height}:force_original_aspect_ratio=decrease",
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black",
            f"fps={fps}",
            "setsar=1",
            "format=yuv420p",
        ]
    else:
        cmd += ["-i", str(src_video)]
        if real_video_dur <= target_dur <= real_video_dur + 2.5:
            speed_factor = 1.0
            hold_dur = max(0.0, target_dur - real_video_dur + 0.25)
            v_filters = [
                "setpts=PTS-STARTPTS",
                f"scale={width}:{height}:force_original_aspect_ratio=decrease",
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black",
                f"fps={fps}",
                "setsar=1",
                "format=yuv420p",
                f"tpad=stop_mode=clone:stop_duration={hold_dur:.6f}",
                f"trim=0:{target_dur:.6f}",
                "setpts=PTS-STARTPTS",
            ]
        elif target_dur <= real_video_dur * 1.65:
            speed_factor = target_dur / real_video_dur
            v_filters = [
                f"setpts={speed_factor:.6f}*(PTS-STARTPTS)",
                f"scale={width}:{height}:force_original_aspect_ratio=decrease",
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black",
                f"fps={fps}",
                "setsar=1",
                "format=yuv420p",
                f"trim=0:{target_dur:.6f}",
                "setpts=PTS-STARTPTS",
            ]
        else:
            speed_factor = 1.65
            stretched_dur = real_video_dur * speed_factor
            hold_dur = max(0.0, target_dur - stretched_dur + 0.25)
            v_filters = [
                f"setpts={speed_factor:.6f}*(PTS-STARTPTS)",
                f"scale={width}:{height}:force_original_aspect_ratio=decrease",
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black",
                f"fps={fps}",
                "setsar=1",
                "format=yuv420p",
                f"tpad=stop_mode=clone:stop_duration={hold_dur:.6f}",
                f"trim=0:{target_dur:.6f}",
                "setpts=PTS-STARTPTS",
            ]

    if "warm_glow" in overlays:
        v_filters.append(
            "eq=gamma=1.03:contrast=1.03:saturation=1.10,"
            "colorbalance=rs=0.045:gs=0.015:bs=-0.035:rm=0.03:gm=0.01:bm=-0.02"
        )

    if "film_grain" in overlays:
        v_filters.append("noise=alls=7:allf=t+u,vignette=PI/5")

    lt_obj = scene.get("lower_third") if isinstance(scene.get("lower_third"), dict) else {}
    hist_date = str(
        scene.get("historical_date")
        or scene.get("date")
        or scene.get("year")
        or lt_obj.get("date")
        or lt_obj.get("title")
        or ""
    ).strip()
    hist_loc = str(
        scene.get("historical_location")
        or scene.get("location")
        or lt_obj.get("location")
        or lt_obj.get("subtitle")
        or ""
    ).strip()
    scene_lt_style = str(scene.get("lower_third_style") or lt_obj.get("style") or lt_style).strip().lower()
    if "ictv" in scene_lt_style or "факт" in scene_lt_style:
        scene_lt_style = "ictv"
    elif "sky" in scene_lt_style:
        scene_lt_style = "sky_news"
    else:
        scene_lt_style = "rossiya24"

    if "lower_thirds" in overlays and (hist_date or hist_loc):
        lt_start = round(cf_dur + 0.35, 3)
        lt_end = round(max(lt_start + 2.5, min(target_dur - 0.35, lt_start + 5.5)), 3)
        if lt_end > lt_start + 0.8:
            v_filters.extend(
                build_lower_third_filters(hist_date, hist_loc, scene_lt_style, width, height, lt_start, lt_end)
            )

    if idx == 0 and cf_dur > 0.0 and trans_type in {"fade", "fadeblack"}:
        v_filters.append(f"fade=t=in:st=0:d={cf_dur:.6f}")
        cf_dur = 0.0

    out_trans = str(scene.get("transition_out_type") or "none").strip().lower()
    out_trans_dur = float(scene.get("transition_out_duration") or 0.0)
    if out_trans in {"fade_to_black", "fadeblack", "fade"} and out_trans_dur > 0.0:
        fade_start = max(0.0, target_dur - out_trans_dur)
        v_filters.append(f"fade=t=out:st={fade_start:.6f}:d={out_trans_dur:.6f}")

    if "particles" in overlays:
        filter_parts = [
            f"[0:v]{','.join(v_filters)}[vbase]",
            f"color=c=black:s=480x270:r={fps}:d={target_dur:.6f},"
            f"noise=alls=45:allf=t,"
            f"lutyuv=y='if(gt(val,244),255,0)':u=128:v=128,"
            f"boxblur=1:1,"
            f"scale={width}:{height}:flags=bilinear[vpart]",
            "[vbase][vpart]blend=all_mode=screen:all_opacity=0.22[vout]",
        ]
    else:
        filter_parts = [f"[0:v]{','.join(v_filters)}[vout]"]

    has_voice = bool(src_voice and src_voice.is_file() and real_voice_dur > 0.0)
    mute_ambient = bool(scene.get("mute_audio", True if has_voice else False))
    ambient_gain = float(scene.get("ambient_gain_db", -18.0))
    voice_gain = float(scene.get("voiceover_gain_db", 0.0))
    video_has_audio = (not is_image) and has_audio_stream(src_video)

    print(
        f"🎬 Сцена #{idx + 1:02d}: xfade={cf_dur:.2f}s | голос={real_voice_dur:.2f}s "
        f"| видео={real_video_dur:.2f}s (x{speed_factor:.2f}) | итог={target_dur:.3f}s"
        + (f" | плашка [{scene_lt_style}]: {hist_date} • {hist_loc}" if ("lower_thirds" in overlays and (hist_date or hist_loc)) else "")
    )

    fade_out_st = max(0.0, target_dur - 0.02)
    delay_filter = f"adelay={lead_in_ms}|{lead_in_ms}," if lead_in_ms > 0 else ""

    amb_tempo = 1.0 / speed_factor
    amb_tempo_filter = f"atempo={amb_tempo:.6f}," if abs(amb_tempo - 1.0) > 0.002 else ""

    if has_voice:
        cmd += ["-i", str(src_voice)]
        filter_parts.append(
            f"[1:a]aresample=48000,aformat=sample_fmts=s16:channel_layouts=stereo,"
            f"volume={voice_gain}dB,{delay_filter}apad,atrim=0:{target_dur:.6f},"
            f"afade=t=in:st=0:d=0.015,afade=t=out:st={fade_out_st:.6f}:d=0.020,asetpts=PTS-STARTPTS[avo]"
        )
        if video_has_audio and not mute_ambient and ambient_gain > -90.0:
            filter_parts.append(
                f"[0:a]aresample=48000,aformat=sample_fmts=s16:channel_layouts=stereo,"
                f"{amb_tempo_filter}volume={ambient_gain}dB,{delay_filter}apad,atrim=0:{target_dur:.6f},"
                f"asetpts=PTS-STARTPTS[aamb]"
            )
            filter_parts.append("[avo][aamb]amix=inputs=2:duration=first:normalize=0[aout]")
        else:
            filter_parts.append("[avo]acopy[aout]")
    elif video_has_audio and not mute_ambient and ambient_gain > -90.0:
        filter_parts.append(
            f"[0:a]aresample=48000,aformat=sample_fmts=s16:channel_layouts=stereo,"
            f"{amb_tempo_filter}volume={ambient_gain}dB,{delay_filter}apad,atrim=0:{target_dur:.6f},"
            f"afade=t=in:st=0:d=0.015,afade=t=out:st={fade_out_st:.6f}:d=0.020,asetpts=PTS-STARTPTS[aout]"
        )
    else:
        filter_parts.append(f"anullsrc=r=48000:cl=stereo:d={target_dur:.6f}[aout]")

    cmd += [
        "-filter_complex", ";".join(filter_parts),
        "-map", "[vout]",
        "-map", "[aout]",
        "-frames:v", str(exact_frames),
        "-r", str(fps),
        "-vsync", "cfr",
        "-c:v", "libx264",
        "-crf", "14",
        "-preset", "superfast",
        "-rc-lookahead", "10",
        "-pix_fmt", "yuv420p",
        "-c:a", "pcm_s16le",
        "-ar", "48000",
        "-ac", "2",
        str(out_path),
    ]
    run_cmd(cmd)
    return out_path, target_dur, cf_dur, cf_dur


def merge_segment_group(
    segments: list[tuple[Path, float, float, str]],
    out_path: Path,
    fps: int,
    crf: int,
    preset: str,
) -> float:
    if len(segments) == 1:
        shutil.copy2(segments[0][0], out_path)
        return segments[0][1]

    cmd = ["ffmpeg", "-y"]
    for seg_path, _, _, _ in segments:
        cmd += ["-i", str(seg_path)]

    filters = []
    for idx in range(len(segments)):
        filters.append(f"[{idx}:v]fps={fps},settb=1/24000,format=yuv420p,setpts=PTS-STARTPTS[iv{idx}]")
        filters.append(f"[{idx}:a]aresample=48000,aformat=sample_fmts=s16:channel_layouts=stereo,asetpts=PTS-STARTPTS[ia{idx}]")

    cur_v = "[iv0]"
    cur_a = "[ia0]"
    total_dur = segments[0][1]

    for idx in range(1, len(segments)):
        _, dur_i, cf_dur_i, trans_type = segments[idx]
        next_v = f"[v{idx}]"
        next_a = f"[a{idx}]"

        if cf_dur_i > 0.0 and trans_type != "cut":
            offset = max(0.0, total_dur - cf_dur_i)
            trim_prev_a = max(0.05, total_dur - cf_dur_i)
            filters.append(
                f"{cur_v}[iv{idx}]xfade=transition={trans_type}:duration={cf_dur_i:.6f}:offset={offset:.6f},settb=1/24000{next_v}"
            )
            filters.append(
                f"{cur_a}atrim=0:{trim_prev_a:.6f},asetpts=PTS-STARTPTS[a_prev_trim{idx}];"
                f"[a_prev_trim{idx}][ia{idx}]concat=n=2:v=0:a=1{next_a}"
            )
            total_dur = offset + dur_i
        else:
            filters.append(f"{cur_v}[iv{idx}]concat=n=2:v=1:a=0,settb=1/24000{next_v}")
            filters.append(f"{cur_a}[ia{idx}]concat=n=2:v=0:a=1{next_a}")
            total_dur += dur_i

        cur_v = next_v
        cur_a = next_a

    exact_frames = max(fps, int(round(total_dur * fps)))
    exact_dur = exact_frames / float(fps)

    cmd += [
        "-filter_complex", ";".join(filters),
        "-map", cur_v,
        "-map", cur_a,
        "-frames:v", str(exact_frames),
        "-r", str(fps),
        "-vsync", "cfr",
        "-c:v", "libx264",
        "-profile:v", "high",
        "-level", "5.1",
        "-crf", str(crf),
        "-preset", preset,
        "-rc-lookahead", "12",
        "-pix_fmt", "yuv420p",
        "-c:a", "pcm_s16le",
        "-ar", "48000",
        "-ac", "2",
        str(out_path),
    ]
    run_cmd(cmd)
    return exact_dur


def finalize_audio_and_container(
    raw_video: Path,
    final_video: Path,
    audio_bitrate: str,
    loudnorm_cfg: dict,
) -> None:
    if loudnorm_cfg.get("enabled", True):
        lufs = float(loudnorm_cfg.get("target_lufs", -14.0))
        tp = float(loudnorm_cfg.get("true_peak_db", -1.0))
        lra = float(loudnorm_cfg.get("lra", 7.0))
        run_cmd([
            "ffmpeg", "-y",
            "-i", str(raw_video),
            "-c:v", "copy",
            "-af", f"loudnorm=I={lufs}:TP={tp}:LRA={lra}",
            "-c:a", "aac",
            "-b:a", audio_bitrate,
            "-ar", "48000",
            "-ac", "2",
            "-movflags", "+faststart",
            str(final_video),
        ])
    else:
        run_cmd([
            "ffmpeg", "-y",
            "-i", str(raw_video),
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", audio_bitrate,
            "-ar", "48000",
            "-ac", "2",
            "-movflags", "+faststart",
            str(final_video),
        ])


def main() -> None:
    manifest_path = Path(
        sys.argv[1] if len(sys.argv) > 1 else os.getenv("MANIFEST_PATH", "manifest.json")
    )
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Манифест не найден: {manifest_path}")

    with manifest_path.open("r", encoding="utf-8") as f:
        manifest = json.load(f)

    scenes = get_normalized_scenes(manifest)
    if not scenes:
        raise ValueError("В манифесте не найден список сцен!")

    overlays, lt_style = parse_overlay_config(manifest)
    print(f"✨ Активные оверлеи: {sorted(overlays) if overlays else 'нет'} | Стиль плашки: {lt_style}")

    render_cfg = manifest.get("render_settings") or {}
    res_str = str(render_cfg.get("resolution", "3840x2160")).split()[0]
    width, height = (int(x) for x in res_str.lower().split("x"))
    fps = int(render_cfg.get("fps", 24))
    crf = min(int(render_cfg.get("crf", 18)), 17)
    audio_bitrate = str(render_cfg.get("audio_bitrate", "320k"))
    folder_id = (
        os.getenv("GDRIVE_FOLDER_ID")
        or os.getenv("EFFECTIVE_FOLDER_ID")
        or manifest.get("gdrive_folder_id")
        or manifest.get("folder_id")
    )

    prepared: list[tuple[Path, float, float, str]] = []
    actual_timings: list[dict] = []
    cursor_t = 0.0

    for idx, scene in enumerate(scenes):
        sc_path, dur, cf_dur, lead_in_sec = prepare_scene(
            scene, idx, width, height, fps, folder_id, overlays, lt_style
        )
        trans_type = str(scene.get("transition") or "cut").strip().lower()
        if trans_type not in VALID_XFADE_TRANSITIONS and trans_type != "cut":
            trans_type = "fade"
        prepared.append((sc_path, dur, cf_dur, trans_type))

        overlap = cf_dur if (idx > 0 and trans_type != "cut" and cf_dur > 0.0) else 0.0
        scene_start = max(0.0, cursor_t - overlap)
        voice_start = scene_start + lead_in_sec
        scene_end = scene_start + dur
        actual_timings.append({
            "scene_index": idx + 1,
            "scene_start": round(scene_start, 3),
            "voice_start": round(voice_start, 3),
            "scene_end": round(scene_end, 3),
            "duration": round(scene_end - scene_start, 3),
        })
        cursor_t = scene_end

    Path("build").mkdir(parents=True, exist_ok=True)
    Path("build/actual_timings.json").write_text(
        json.dumps(actual_timings, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    project_name = manifest.get("project_name", "output")
    out_dir = Path("output")
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_master = Path("build/raw_master.mkv")
    final_video = out_dir / f"{project_name}.mp4"
    loudnorm = render_cfg.get("loudness_normalization") or {}

    if len(prepared) <= BATCH_SIZE:
        total_dur = merge_segment_group(
            prepared, raw_master, fps=fps, crf=crf, preset="faster"
        )
    else:
        batches_dir = Path("build/batches")
        batches_dir.mkdir(parents=True, exist_ok=True)
        batch_segments: list[tuple[Path, float, float, str]] = []

        for b_idx, start in enumerate(range(0, len(prepared), BATCH_SIZE)):
            chunk = prepared[start : start + BATCH_SIZE]
            in_cf_dur = chunk[0][2]
            in_trans = chunk[0][3]
            chunk_for_merge = [(chunk[0][0], chunk[0][1], 0.0, "cut")] + chunk[1:]
            batch_out = batches_dir / f"batch_{b_idx:02d}.mkv"
            batch_dur = merge_segment_group(
                chunk_for_merge, batch_out, fps=fps, crf=15, preset="superfast"
            )
            batch_segments.append((batch_out, batch_dur, in_cf_dur, in_trans))
            print(f"✅ Batch {b_idx + 1} merged ({len(chunk)} scenes, {batch_dur:.3f}s)")

        total_dur = merge_segment_group(
            batch_segments, raw_master, fps=fps, crf=crf, preset="faster"
        )

    finalize_audio_and_container(raw_master, final_video, audio_bitrate, loudnorm)
    print(f"✅ Render complete: {final_video} ({total_dur:.3f}s)")


if __name__ == "__main__":
    main()
