import json
import os
import subprocess

MANIFEST_FILE = "manifest.json"
ASSETS_DIR = "assets"
WORK_DIR = "temp_scenes"

os.makedirs(ASSETS_DIR, exist_ok=True)
os.makedirs(WORK_DIR, exist_ok=True)


def run(cmd):
    print(f"RUN: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def safe_remove(path):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def get_duration(file_path):
    res = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", file_path
    ]).decode().strip()
    return float(res)


def ensure_asset(filename, folder_id=""):
    local_path = os.path.join(ASSETS_DIR, filename)
    if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
        return local_path
    if os.path.exists(filename) and os.path.getsize(filename) > 0:
        return filename

    print(f"Скачиваем {filename} из Google Drive...")
    if folder_id:
        subprocess.run([
            "rclone", "copy", f"gdrive:{filename}", ASSETS_DIR,
            "--drive-root-folder-id", folder_id, "-v"
        ], check=False)

    if not os.path.exists(local_path):
        run([
            "rclone", "copy", "gdrive:", ASSETS_DIR,
            "--include", filename, "-v"
        ])

    if not os.path.exists(local_path):
        raise FileNotFoundError(f"Файл {filename} не найден ни локально, ни на Google Диске!")
    return local_path


def prepare_scene(scene, idx, width, height, fps, folder_id):
    vid_name = scene.get("file_name") or scene.get("video_file") or scene.get("asset")
    if not vid_name:
        raise KeyError(f"В сцене #{idx + 1} не указано имя видеофайла (file_name / video_file)")

    vid_path = ensure_asset(vid_name, folder_id)
    audio_name = scene.get("audio_file") or scene.get("voiceover_file")
    out_scene = os.path.join(WORK_DIR, f"scene_{idx:03d}.mp4")

    # Проверяем наличие аудиодорожки в исходном видео
    probe_audio = subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "a",
        "-show_entries", "stream=codec_type", "-of", "csv=p=0", vid_path
    ]).decode().strip()

    vf_chain = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},fps={fps},format=yuv420p"
    )

    if audio_name:
        audio_path = ensure_asset(audio_name, folder_id)
        aud_dur = get_duration(audio_path)
        vid_dur = get_duration(vid_path)
        target_dur = float(scene.get("duration_sec") or max(vid_dur, aud_dur))

        # Если видео короче озвучки, зацикливаем или подгоняем длительность под звук
        cmd = ["ffmpeg", "-y"]
        if vid_dur < target_dur:
            cmd.extend(["-stream_loop", "-1"])
        cmd.extend([
            "-i", vid_path, "-i", audio_path,
            "-t", str(target_dur),
            "-vf", vf_chain,
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "320k",
            out_scene
        ])
        run(cmd)
    else:
        target_dur = float(scene.get("duration_sec") or get_duration(vid_path))
        if probe_audio:
            run([
                "ffmpeg", "-y", "-i", vid_path,
                "-t", str(target_dur),
                "-vf", vf_chain,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "320k",
                out_scene
            ])
        else:
            # Добавляем тихую стереодорожку, если исходник без звука
            run([
                "ffmpeg", "-y", "-i", vid_path,
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                "-t", str(target_dur),
                "-vf", vf_chain,
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "320k",
                out_scene
            ])

    dur = get_duration(out_scene)
    trans_out = scene.get("transition_out") or {}
    if isinstance(trans_out, dict):
        cf_dur = float(trans_out.get("duration_sec", 0)) if trans_out.get("type") not in (None, "none") else 0.0
    else:
        cf_dur = float(scene.get("crossfade_sec", 0))

    return out_scene, dur, cf_dur


def main():
    with open(MANIFEST_FILE, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    exp = manifest.get("export_settings", {})
    width = exp.get("width", 1920)
    height = exp.get("height", 1080)
    fps = exp.get("fps", 24)
    v_codec = exp.get("video_codec", "libx264")
    preset = exp.get("preset", "veryfast")
    crf = str(exp.get("crf", 20))
    a_bitrate = exp.get("audio_bitrate", "320k")
    out_filename = exp.get("output_filename", "scenes_master.mp4")
    folder_id = manifest.get("cloud_export", {}).get("target_folder_id", "")

    scenes = manifest.get("scenes") or manifest.get("timeline_slots") or []
    if not scenes:
        raise ValueError("В манифесте не найден список сцен ('scenes' или 'timeline_slots')!")

    prepared = []
    for idx, scene in enumerate(scenes):
        sc_path, dur, cf_dur = prepare_scene(scene, idx, width, height, fps, folder_id)
        prepared.append((sc_path, dur, cf_dur))

    print(f"=== Сборка {len(prepared)} сцен в один проход: {out_filename} ===")
    inputs_cmd = []
    for path, _, _ in prepared:
        inputs_cmd.extend(["-i", path])

    filter_parts = []
    prev_v = "[0:v]"
    prev_a = "[0:a]"
    offset = prepared[0][1]

    for idx in range(1, len(prepared)):
        cf_dur = prepared[idx - 1][2]
        out_v = f"[v{idx}]"
        out_a = f"[a{idx}]"
        if cf_dur > 0 and offset > cf_dur:
            offset_adj = offset - cf_dur
            filter_parts.append(f"{prev_v}[{idx}:v]xfade=transition=fade:duration={cf_dur}:offset={offset_adj}{out_v}")
            filter_parts.append(f"{prev_a}[{idx}:a]acrossfade=d={cf_dur}{out_a}")
            offset = offset_adj + prepared[idx][1]
        else:
            filter_parts.append(f"{prev_v}[{idx}:v]concat=n=2:v=1:a=0{out_v}")
            filter_parts.append(f"{prev_a}[{idx}:a]concat=n=2:v=0:a=1{out_a}")
            offset += prepared[idx][1]
        prev_v = out_v
        prev_a = out_a

    # Подмешиваем фоновую музыку (если включена в манифесте) и нормализуем громкость
    bg_music = manifest.get("background_music") or manifest.get("audio_rules", {}).get("continuous_bed", {})
    if isinstance(bg_music, dict) and bg_music.get("enabled") and bg_music.get("source_file"):
        bg_path = ensure_asset(bg_music["source_file"], folder_id)
        bg_idx = len(prepared)
        inputs_cmd.extend(["-stream_loop", "-1", "-i", bg_path])
        gain_db = float(bg_music.get("volume_gain_db", -14.0))
        filter_parts.append(f"[{bg_idx}:a]volume={gain_db}dB[bg_a]")
        filter_parts.append(f"{prev_a}[bg_a]amix=inputs=2:duration=first:dropout_transition=2[amixed]")
        filter_parts.append("[amixed]loudnorm=I=-16.0:TP=-3.0:LRA=11.0[afinal]")
    else:
        filter_parts.append(f"{prev_a}loudnorm=I=-16.0:TP=-3.0:LRA=11.0[afinal]")

    full_fc = ";".join(filter_parts)

    run([
        "ffmpeg", "-y",
        *inputs_cmd,
        "-filter_complex", full_fc,
        "-map", prev_v, "-map", "[afinal]",
        "-c:v", v_codec, "-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", a_bitrate, "-ar", "48000",
        out_filename
    ])

    # Очищаем временные файлы сцен
    for path, _, _ in prepared:
        safe_remove(path)

    with open("final_output_name.txt", "w", encoding="utf-8") as f:
        f.write(out_filename)


if __name__ == "__main__":
    main()
