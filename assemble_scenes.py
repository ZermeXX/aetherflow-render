import json
import os
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
    candidates = [
        work_dir / base_name,
        work_dir / filename,
        Path("assets") / filename,
        Path("assets") / base_name,
        Path("input") / filename,
        Path(filename),
    ]
    for path in candidates:
        if path.is_file():
            return path

    if folder_id:
        remote = os.getenv("GDRIVE_REMOTE", "gdrive")
        dest = work_dir / base_name
        dest.parent.mkdir(parents=True, exist_ok=True)
        run_cmd([
            "rclone", "copyto",
            f"{remote}:{filename}",
            str(dest),
            "--drive-root-folder-id", folder_id,
        ])
        if dest.is_file():
            return dest

    return None


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

    # Привязываем длительность перехода к точному числу кадров (fps)
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

    # Жёстко квантуем длину сцены по сетке кадров (исключает дрейф между аудио и видео)
    exact_frames = max(fps, int(round(raw_target_dur * fps)))
    target_dur = exact_frames / float(fps)

    real_video_dur = target_dur if is_image else get_media_duration(src_video)
    if real_video_dur <= 0.05:
        real_video_dur = target_dur

    out_path = work_dir / f"norm_{idx + 1:03d}.mkv"

    cmd = ["ffmpeg", "-y"]
    speed_factor = 1.0  # >1.0 означает замедление видео

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
        # Если видео отстаёт от голоса не более чем на 2.5 сек (напр. 10с клип vs 12с речь),
        # оставляем естественную скорость 1.0x (чтобы анимации/кнопки не замедлялись) + мягкий стоп-кадр в конце.
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

    if idx == 0 and cf_dur > 0.0 and trans_type in {"fade", "fadeblack"}:
        v_filters.append(f"fade=t=in:st=0:d={cf_dur:.6f}")
        cf_dur = 0.0

    out_trans = str(scene.get("transition_out_type") or "none").strip().lower()
    out_trans_dur = float(scene.get("transition_out_duration") or 0.0)
    if out_trans in {"fade_to_black", "fadeblack", "fade"} and out_trans_dur > 0.0:
        fade_start = max(0.0, target_dur - out_trans_dur)
        v_filters.append(f"fade=t=out:st={fade_start:.6f}:d={out_trans_dur:.6f}")

    filter_parts = [f"[0:v]{','.join(v_filters)}[vout]"]

    has_voice = bool(src_voice and src_voice.is_file() and real_voice_dur > 0.0)
    # Если в сцене есть голос диктора, встроенный звук клипа по умолчанию глушим,
    # если только в манифесте явно не указано mute_audio: false
    mute_ambient = bool(scene.get("mute_audio", True if has_voice else False))
    ambient_gain = float(scene.get("ambient_gain_db", -18.0))
    voice_gain = float(scene.get("voiceover_gain_db", 0.0))
    video_has_audio = (not is_image) and has_audio_stream(src_video)

    print(
        f"🎬 Сцена #{idx + 1:02d}: xfade={cf_dur:.2f}s | голос={real_voice_dur:.2f}s "
        f"| видео={real_video_dur:.2f}s (x{speed_factor:.2f}) | итог={target_dur:.3f}s ({exact_frames} кадров)"
    )

    fade_out_st = max(0.0, target_dur - 0.02)
    delay_filter = f"adelay={lead_in_ms}|{lead_in_ms}," if lead_in_ms > 0 else ""

    # Встроенный звук клипа [0:a] синхронизируется по скорости (atempo) и задержке (adelay) с картинкой [0:v]
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
            scene, idx, width, height, fps, folder_id
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
