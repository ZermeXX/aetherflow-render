import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess

# Lower-thirds берём из общего модуля (там уже починены пути шрифтов).
from lower_third_overlay import apply_lower_third_to_clip


def _resolve_media_path(filename: str, assets_dir: Path):
    if not filename or not str(filename).strip():
        return None
    name = str(filename).strip()
    direct = assets_dir / name
    if direct.is_file():
        return direct
    if assets_dir.is_dir():
        for f in assets_dir.iterdir():
            if f.is_file() and f.name.lower() == name.lower():
                return f
    fallback = Path.cwd() / name
    if fallback.is_file():
        return fallback
    return None


def _get_media_duration_sec(media_path: Path) -> float:
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(media_path),
    ], text=True).strip()
    return float(out)


def _has_audio_stream(media_path: Path) -> bool:
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "a",
        "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(media_path),
    ], text=True).strip()
    return "audio" in out


def _ensure_asset(filename: str, assets_dir: Path, folder_id: str):
    """Локальный файл или докачка с Google Drive (narrative-workflow раньше не качал ассеты)."""
    found = _resolve_media_path(filename, assets_dir)
    if found is not None:
        return found
    name = str(filename).strip()
    assets_dir.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {name} from Google Drive...", flush=True)
    if folder_id:
        subprocess.run([
            "rclone", "copy", f"gdrive:{name}", str(assets_dir),
            "--drive-root-folder-id", folder_id, "--ignore-case", "-v",
        ], check=False)
    found = _resolve_media_path(name, assets_dir)
    if found is None:
        subprocess.run([
            "rclone", "copy", "gdrive:", str(assets_dir),
            "--include", name, "--ignore-case", "-v",
        ], check=False)
        found = _resolve_media_path(name, assets_dir)
    if found is None:
        raise FileNotFoundError(f"Asset not found locally or on Google Drive: {name}")
    return found


def _prepare_segment(entry: dict, assets_dir: Path, temp_dir: Path, out_w: int, out_h: int,
                     fps: int, lt_cfg: dict, export_cfg: dict, folder_id: str) -> Path:
    idx = int(entry.get("scene_index") or entry.get("index") or 0)
    assets = entry.get("assets") or {}
    raw_vid = (entry.get("video_file") or entry.get("file_name")
               or assets.get("video_file") or assets.get("image_reference") or "")
    raw_voice = entry.get("voiceover_file") or entry.get("audio_file") or assets.get("voiceover_file") or ""
    vid_path = _ensure_asset(raw_vid, assets_dir, folder_id)
    voice_path = _ensure_asset(raw_voice, assets_dir, folder_id) if str(raw_voice).strip() else None

    voice_start = float(entry.get("voice_start", 0.2))
    delay_ms = max(0, int(round(voice_start * 1000)))
    target_dur = float(entry.get("duration_sec") or entry.get("duration") or _get_media_duration_sec(vid_path))
    if voice_path is not None:
        target_dur = max(target_dur, _get_media_duration_sec(voice_path) + voice_start + 0.35)

    vid_has_audio = _has_audio_stream(vid_path)
    ambient_gain_db = float(entry.get("ambient_gain_db", -18.0))
    mute_video_audio = bool(entry.get("mute_audio", False))
    use_ambient = vid_has_audio and not mute_video_audio and voice_path is None
    mix_ambient = vid_has_audio and not mute_video_audio and voice_path is not None

    base_clip = temp_dir / f"seg_{idx:03d}_base.mp4"
    vf = (f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
          f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2,fps={fps},setsar=1,format=yuv420p")
    v_codec = export_cfg.get("video_codec", "libx264")
    preset = str(export_cfg.get("preset", "veryfast"))
    crf = str(export_cfg.get("crf", 18))
    a_kbps = int(export_cfg.get("audio_bitrate_kbps", 320))
    enc = ["-c:v", v_codec, "-profile:v", "high", "-crf", crf, "-preset", preset,
           "-pix_fmt", "yuv420p", "-r", str(fps), "-g", str(fps * 2), "-keyint_min", str(fps * 2),
           "-c:a", "aac", "-b:a", f"{a_kbps}k", "-ar", "48000", "-ac", "2",
           "-video_track_timescale", "24000", "-t", f"{target_dur:.3f}"]

    inputs = ["-stream_loop", "-1", "-i", str(vid_path)]
    if voice_path is not None:
        inputs += ["-i", str(voice_path)]
    need_silence = (not vid_has_audio) or (mute_video_audio and voice_path is None)
    silence_idx = 2 if voice_path is not None else 1
    if need_silence:
        inputs += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]

    parts = [f"[0:v]{vf},trim=duration={target_dur:.3f},setpts=PTS-STARTPTS[v]"]
    if voice_path is not None:
        parts.append(f"[1:a]aresample=48000,adelay={delay_ms}|{delay_ms},apad,"
                     f"atrim=0:{target_dur:.3f},asetpts=PTS-STARTPTS[vox]")
        if mix_ambient:
            parts.append(f"[0:a]volume={ambient_gain_db}dB,apad,atrim=0:{target_dur:.3f},"
                         f"asetpts=PTS-STARTPTS[amb]")
            parts.append("[amb][vox]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]")
        else:
            parts.append("[vox]anull[aout]")
    elif use_ambient:
        parts.append(f"[0:a]volume={ambient_gain_db}dB,apad,atrim=0:{target_dur:.3f},"
                     f"asetpts=PTS-STARTPTS[aout]")
    else:
        parts.append(f"[{silence_idx}:a]atrim=0:{target_dur:.3f},asetpts=PTS-STARTPTS[aout]")

    cmd = ["ffmpeg", "-y"] + inputs + ["-filter_complex", ";".join(parts),
           "-map", "[v]", "-map", "[aout]"] + enc + [str(base_clip)]
    print(f"[scene {idx}] dur={target_dur:.3f}s voice_delay={delay_ms}ms", flush=True)
    subprocess.run(cmd, check=True)

    text_overlay = (entry.get("text_overlay") or "").strip()
    if lt_cfg.get("enabled", True) and text_overlay:
        show_sec = lt_cfg.get("show_sec", [0.5, 5.5])
        emblem_raw = lt_cfg.get("emblem_path", "aether_art_150x150.png")
        emblem_path = _resolve_media_path(emblem_raw, assets_dir)
        ov_clip = temp_dir / f"seg_{idx:03d}_ov.mp4"
        apply_lower_third_to_clip(
            input_video_path=str(base_clip),
            output_video_path=str(ov_clip),
            text_overlay=text_overlay,
            fps=fps,
            emblem_path=str(emblem_path) if emblem_path else emblem_raw,
            show_start=float(show_sec[0]),
            show_end=float(show_sec[1]),
            anim_dur=float(lt_cfg.get("anim_sec", 0.55)),
            work_dir=str(temp_dir / f"lt_frames_{idx:03d}"),
        )
        return ov_clip
    return base_clip


def assemble_from_manifest(manifest_path: str, assets_dir: str, output_path=None) -> Path:
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    export_cfg = manifest.get("export_settings", {}) or {}
    res_mode = str(export_cfg.get("resolution", "4k")).lower()
    if res_mode in ("1080p", "fhd"):
        out_w, out_h = 1920, 1080
    elif res_mode in ("720p", "hd", "draft"):
        out_w, out_h = 1280, 720
    elif res_mode in ("2k", "qhd", "1440p"):
        out_w, out_h = 2560, 1440
    else:
        out_w, out_h = 3840, 2160
    fps = int(export_cfg.get("fps", 24))
    lt_cfg = export_cfg.get("lower_third_config", {}) or {}
    folder_id = str(manifest.get("gdrive_folder_id") or manifest.get("folder_id")
                    or (manifest.get("cloud_export", {}) or {}).get("target_folder_id")
                    or os.environ.get("GDRIVE_FOLDER_ID", "")).strip()
    items = manifest.get("timeline") or manifest.get("scenes") or []
    if not items:
        raise ValueError("Manifest contains no scenes or timeline entries.")

    out_name = output_path or manifest.get("project_name") or "Histoviva_4K.mp4"
    final_out = Path(out_name)
    if final_out.suffix.lower() != ".mp4":
        final_out = final_out.with_suffix(".mp4")
    if final_out.parent == Path("."):
        final_out = Path("output") / final_out
    final_out.parent.mkdir(parents=True, exist_ok=True)

    assets_root = Path(assets_dir)
    temp_dir = final_out.parent / f"_build_{final_out.stem}"
    if temp_dir.exists():
        shutil.rmtree(temp_dir, ignore_errors=True)
    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        prepared_clips = []
        actual_timings = []
        cur_t = 0.0
        for n, entry in enumerate(items, 1):
            entry.setdefault("scene_index", n)
            seg_path = _prepare_segment(entry, assets_root, temp_dir, out_w, out_h, fps,
                                        lt_cfg, export_cfg, folder_id)
            seg_dur = _get_media_duration_sec(seg_path)
            vs = float(entry.get("voice_start", 0.2))
            actual_timings.append({
                "scene_index": n,
                "scene_start": round(cur_t, 3),
                "voice_start": round(cur_t + vs, 3),
                "scene_end": round(cur_t + seg_dur, 3),
                "duration": round(seg_dur, 3),
            })
            cur_t += seg_dur
            prepared_clips.append(seg_path)

        Path("build").mkdir(parents=True, exist_ok=True)
        Path("build/actual_timings.json").write_text(
            json.dumps(actual_timings, ensure_ascii=False, indent=2), encoding="utf-8")

        concat_list = temp_dir / "concat.txt"
        with open(concat_list, "w", encoding="utf-8") as cf:
            for p in prepared_clips:
                cf.write(f"file '{p.resolve().as_posix()}'\n")

        concat_cmd = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
            "-c:v", export_cfg.get("video_codec", "libx264"), "-profile:v", "high",
            "-crf", str(export_cfg.get("crf", 18)),
            "-preset", str(export_cfg.get("preset", "veryfast")),
            "-pix_fmt", "yuv420p", "-r", str(fps),
            "-c:a", "aac", "-b:a", f"{int(export_cfg.get('audio_bitrate_kbps', 320))}k",
            "-ar", "48000", "-ac", "2",
            "-af", "loudnorm=I=-14:TP=-1:LRA=11",
            "-movflags", "+faststart",
            str(final_out),
        ]
        subprocess.run(concat_cmd, check=True)
        print(f"Assembled: {final_out} | {cur_t:.2f}s | {len(prepared_clips)} scenes", flush=True)
        return final_out
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description="Assemble documentary from manifest.json")
    parser.add_argument("manifest_pos", nargs="?", default=None, help="Path to manifest.json (positional)")
    parser.add_argument("--manifest", default="manifest.json", help="Path to manifest.json")
    parser.add_argument("--assets-dir", default="assets", help="Directory with scene videos / voices / emblem")
    parser.add_argument("--output", default=None, help="Output video path")
    args = parser.parse_args()
    manifest_path = args.manifest_pos or args.manifest
    result = assemble_from_manifest(manifest_path, args.assets_dir, args.output)
    print(f"Assembled video: {result}")


if __name__ == "__main__":
    main()
