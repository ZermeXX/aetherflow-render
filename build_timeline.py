import json
import os
import subprocess
import math

MANIFEST_FILE = "manifest.json"
ASSETS_DIR = "assets"
WORK_DIR = "temp_render"

os.makedirs(ASSETS_DIR, exist_ok=True)
os.makedirs(WORK_DIR, exist_ok=True)


def run(cmd):
    print(f"RUN: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def ensure_asset(filename, folder_id):
    local_path = os.path.join(ASSETS_DIR, filename)
    if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
        return local_path
    if os.path.exists(filename) and os.path.getsize(filename) > 0:
        return filename

    print(f"Скачиваем {filename} из Google Drive...")
    # Ищем сначала в целевой папке проекта, затем по всему диску
    cmd_folder = [
        "rclone", "copy", f"gdrive:{filename}", ASSETS_DIR,
        "--drive-root-folder-id", folder_id, "-v"
    ]
    subprocess.run(cmd_folder, check=False)

    if not os.path.exists(local_path):
        cmd_global = [
            "rclone", "copy", "gdrive:", ASSETS_DIR,
            "--include", filename, "-v"
        ]
        run(cmd_global)

    if not os.path.exists(local_path):
        raise FileNotFoundError(f"Исходный файл {filename} не найден ни в репозитории, ни на Google Диске!")
    return local_path


def build_seamless_loop(src_file, target_sec, cf_sec, out_file, width, height, fps):
    # Нормализуем исходник под единые параметры (разрешение, fps, аудио)
    norm_clip = os.path.join(WORK_DIR, f"norm_{os.path.basename(src_file)}")
    run([
        "ffmpeg", "-y", "-i", src_file,
        "-vf", f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},fps={fps},format=yuv420p",
        "-c:v", "libx264", "-preset", "fast", "-crf", "16",
        "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "320k",
        norm_clip
    ])

    # Получаем точную длительность клипа
    res = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", norm_clip
    ]).decode().strip()
    dur = float(res)

    if cf_sec <= 0 or dur <= cf_sec * 2:
        effective_dur = dur
        unit_clip = norm_clip
    else:
        # Делаем 1 бесшовный модуль: хвост клипа (cf_sec) плавно переходит в его начало
        effective_dur = dur - cf_sec
        unit_clip = os.path.join(WORK_DIR, f"unit_{os.path.basename(src_file)}")
        # Сдвигаем начало на cf_sec и накладываем кроссфейд конца на начало
        filter_complex = (
            f"[0:v]trim=start={cf_sec},setpts=PTS-STARTPTS[main_v];"
            f"[0:v]trim=end={cf_sec},setpts=PTS-STARTPTS[head_v];"
            f"[main_v][head_v]xfade=transition=fade:duration={cf_sec}:offset={effective_dur - cf_sec}[v];"
            f"[0:a]atrim=start={cf_sec},asetpts=PTS-STARTPTS[main_a];"
            f"[0:a]atrim=end={cf_sec},asetpts=PTS-STARTPTS[head_a];"
            f"[main_a][head_a]acrossfade=d={cf_sec}[a]"
        )
        run([
            "ffmpeg", "-y", "-i", norm_clip,
            "-filter_complex", filter_complex,
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "16",
            "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "320k",
            unit_clip
        ])

    # Быстро размножаем бесшовный модуль через concat -c copy до нужной длины слота
    repeats = math.ceil(target_sec / effective_dur) + 1
    list_txt = os.path.join(WORK_DIR, f"list_{os.path.basename(out_file)}.txt")
    with open(list_txt, "w") as f:
        for _ in range(repeats):
            f.write(f"file '{os.path.abspath(unit_clip)}'\n")

    run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_txt,
        "-t", str(target_sec), "-c", "copy", out_file
    ])


def apply_inserts(base_video, inserts, slot_start_sec, out_video, width, height, fps, folder_id):
    if not inserts:
        os.replace(base_video, out_video)
        return

    current_vid = base_video
    for idx, ins in enumerate(inserts):
        ins_file = ensure_asset(ins["file_name"], folder_id)
        rel_start = float(ins["trigger_at_sec"]) - slot_start_sec
        ins_dur = float(ins["source_clip_duration_sec"])
        fade_in = float(ins.get("transition_duration_sec", 1.0))
        t_out = ins.get("transition_out", {})
        fade_out = float(t_out.get("duration_sec", 0)) if t_out.get("type") != "none" else 0.0

        step_out = os.path.join(WORK_DIR, f"ins_{idx}_{os.path.basename(out_video)}")

        # Готовим видео и аудио врезки с плавным появлением/исчезновением (dissolve)
        v_chain = (
            f"[1:v]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
            f"fps={fps},format=yuva420p"
        )
        if fade_in > 0:
            v_chain += f",fade=t=in:st=0:d={fade_in}:alpha=1"
        if fade_out > 0:
            v_chain += f",fade=t=out:st={max(0, ins_dur - fade_out)}:d={fade_out}:alpha=1"
        v_chain += f",setpts=PTS-STARTPTS+{rel_start}/TB[ov_v];"

        delay_ms = int(rel_start * 1000)
        a_chain = f"[1:a]aresample=48000"
        if fade_in > 0:
            a_chain += f",afade=t=in:st=0:d={fade_in}"
        if fade_out > 0:
            a_chain += f",afade=t=out:st={max(0, ins_dur - fade_out)}:d={fade_out}"
        a_chain += f",adelay={delay_ms}|{delay_ms}[ov_a];"

        filter_complex = (
            f"{v_chain}{a_chain}"
            f"[0:v][ov_v]overlay=0:0:eof_action=pass[outv];"
            f"[0:a][ov_a]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[outa]"
        )

        run([
            "ffmpeg", "-y", "-i", current_vid, "-i", ins_file,
            "-filter_complex", filter_complex,
            "-map", "[outv]", "-map", "[outa]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "17",
            "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "320k",
            step_out
        ])
        current_vid = step_out

    os.replace(current_vid, out_video)


def main():
    with open(MANIFEST_FILE, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    exp = manifest["export_settings"]
    width = exp.get("width", 3840)
    height = exp.get("height", 2160)
    fps = exp.get("fps", 24)
    v_codec = exp.get("video_codec", "libx265")
    crf = str(exp.get("crf", 18))
    a_bitrate = exp.get("audio_bitrate", "320k")
    out_filename = exp.get("output_filename", "output.mp4")
    folder_id = manifest.get("cloud_export", {}).get("target_folder_id", "")

    slots = manifest["timeline_slots"]
    built_slots = []

    for i, slot in enumerate(slots):
        slot_id = slot["slot_id"]
        base = slot["base_content"]
        src_path = ensure_asset(base["file_name"], folder_id)

        # Добавляем запас длительности на исходящий 90-сек кроссфейд между слотами
        trans_out = slot.get("slot_transition_out", {})
        cf_out = float(trans_out.get("duration_sec", 0)) if trans_out.get("type") == "crossfade" else 0.0
        raw_dur = float(slot["slot_duration_sec"]) + cf_out

        raw_slot_path = os.path.join(WORK_DIR, f"{slot_id}_raw.mp4")
        final_slot_path = os.path.join(WORK_DIR, f"{slot_id}_final.mp4")

        build_seamless_loop(
            src_path, raw_dur, float(base.get("loop_crossfade_sec", 1.0)),
            raw_slot_path, width, height, fps
        )
        apply_inserts(
            raw_slot_path, slot.get("inserts", []), float(slot["start_sec"]),
            final_slot_path, width, height, fps, folder_id
        )
        built_slots.append((final_slot_path, cf_out))

    # Последовательно склеиваем все 6 слотов с плавными 90-сек кроссфейдами и финальной нормализацией LUFS
    merged_video = built_slots[0][0]
    current_offset = float(slots[0]["slot_duration_sec"])

    for idx in range(1, len(built_slots)):
        next_vid, next_cf = built_slots[idx]
        cf_dur = built_slots[idx - 1][1]
        step_merged = os.path.join(WORK_DIR, f"merged_{idx}.mp4")

        if cf_dur > 0:
            fc = (
                f"[0:v][1:v]xfade=transition=fade:duration={cf_dur}:offset={current_offset}[v];"
                f"[0:a][1:a]acrossfade=d={cf_dur}[a]"
            )
            run([
                "ffmpeg", "-y", "-i", merged_video, "-i", next_vid,
                "-filter_complex", fc,
                "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-preset", "fast", "-crf", "17",
                "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "320k",
                step_merged
            ])
        else:
            list_f = os.path.join(WORK_DIR, f"concat_{idx}.txt")
            with open(list_f, "w") as lf:
                lf.write(f"file '{os.path.abspath(merged_video)}'\n")
                lf.write(f"file '{os.path.abspath(next_vid)}'\n")
            run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_f, "-c", "copy", step_merged])

        merged_video = step_merged
        current_offset += float(slots[idx]["slot_duration_sec"])

    # Финальное кодирование в целевой кодек (HEVC/H.265 CRF 18) + нормализация громкости (-16 LUFS, -3 dBTP)
    print(f"=== Финальный рендер {out_filename} ({v_codec}, CRF {crf}, -16 LUFS) ===")
    run([
        "ffmpeg", "-y", "-i", merged_video,
        "-af", "loudnorm=I=-16.0:TP=-3.0:LRA=11.0",
        "-c:v", v_codec, "-preset", "medium", "-crf", crf, "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", a_bitrate, "-ar", "48000",
        out_filename
    ])

    with open("final_output_name.txt", "w") as f:
        f.write(out_filename)


if __name__ == "__main__":
    main()
