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


def ensure_asset(filename, folder_id):
    local_path = os.path.join(ASSETS_DIR, filename)
    if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
        return local_path
    if os.path.exists(filename) and os.path.getsize(filename) > 0:
        return filename

    print(f"Скачиваем {filename} из Google Drive...")
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
    norm_clip = os.path.join(WORK_DIR, f"norm_{os.path.basename(src_file)}")
    run([
        "ffmpeg", "-y", "-i", src_file,
        "-vf", f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},fps={fps},format=yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "320k",
        norm_clip
    ])

    dur = get_duration(norm_clip)

    if cf_sec <= 0 or dur <= cf_sec * 2:
        effective_dur = dur
        unit_clip = norm_clip
    else:
        effective_dur = dur - cf_sec
        unit_clip = os.path.join(WORK_DIR, f"unit_{os.path.basename(src_file)}")
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
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "320k",
            unit_clip
        ])
        safe_remove(norm_clip)

    repeats = math.ceil(target_sec / effective_dur) + 1
    list_txt = os.path.join(WORK_DIR, f"list_{os.path.basename(out_file)}.txt")
    with open(list_txt, "w") as f:
        for _ in range(repeats):
            f.write(f"file '{os.path.abspath(unit_clip)}'\n")

    run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_txt,
        "-t", str(target_sec), "-c", "copy", out_file
    ])
    safe_remove(unit_clip)
    safe_remove(list_txt)


def apply_inserts(base_video, inserts, slot_start_sec, out_video, width, height, fps, folder_id):
    if not inserts:
        os.replace(base_video, out_video)
        return

    current_vid = base_video
    for idx, ins in enumerate(inserts):
        ins_file = ensure_asset(ins["file_name"], folder_id)
        rel_start = max(0.0, float(ins.get("trigger_at_sec", slot_start_sec)) - slot_start_sec)
        
        # Определяем длительность врезки напрямую через ffprobe, если ключа нет в JSON
        raw_dur = ins.get("source_clip_duration_sec") or ins.get("duration_sec") or ins.get("duration")
        ins_dur = float(raw_dur) if raw_dur is not None else get_duration(ins_file)

        fade_in = float(ins.get("transition_duration_sec", 1.0))
        t_out = ins.get("transition_out") or {}
        if isinstance(t_out, dict):
            fade_out = float(t_out.get("duration_sec", 0)) if t_out.get("type") != "none" else 0.0
        else:
            fade_out = float(ins.get("transition_out_duration_sec", 1.0))

        step_out = os.path.join(WORK_DIR, f"ins_{idx}_{os.path.basename(out_video)}")

        v_chain = (
            f"[1:v]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
            f"fps={fps},format=yuva420p"
        )
        if fade_in > 0:
            v_chain += f",fade=t=in:st=0:d={fade_in}:alpha=1"
        if fade_out > 0:
            v_chain += f",fade=t=out:st={max(0.0, ins_dur - fade_out)}:d={fade_out}:alpha=1"
        v_chain += f",setpts=PTS-STARTPTS+{rel_start}/TB[ov_v];"

        delay_ms = int(rel_start * 1000)
        a_chain = "[1:a]aresample=48000"
        if fade_in > 0:
            a_chain += f",afade=t=in:st=0:d={fade_in}"
        if fade_out > 0:
            a_chain += f",afade=t=out:st={max(0.0, ins_dur - fade_out)}:d={fade_out}"
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
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "320k",
            step_out
        ])
        safe_remove(current_vid)
        current_vid = step_out

    os.replace(current_vid, out_video)


def main():
    with open(MANIFEST_FILE, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    exp = manifest.get("export_settings", {})
    width = exp.get("width", 3840)
    height = exp.get("height", 2160)
    fps = exp.get("fps", 24)
    v_codec = exp.get("video_codec", "libx264")
    preset = exp.get("preset", "veryfast")
    crf = str(exp.get("crf", 20))
    a_bitrate = exp.get("audio_bitrate", "320k")
    out_filename = exp.get("output_filename", "output.mp4")
    folder_id = manifest.get("cloud_export", {}).get("target_folder_id", "")

    slots = manifest["timeline_slots"]
    built_slots = []

    for i, slot in enumerate(slots):
        slot_id = slot["slot_id"]
        base = slot["base_content"]
        src_path = ensure_asset(base["file_name"], folder_id)

        trans_out = slot.get("slot_transition_out") or {}
        cf_out = float(trans_out.get("duration_sec", 0)) if trans_out.get("type") == "crossfade" else 0.0
        slot_dur = float(slot["slot_duration_sec"])
        raw_dur = slot_dur + cf_out

        raw_slot_path = os.path.join(WORK_DIR, f"{slot_id}_raw.mp4")
        final_slot_path = os.path.join(WORK_DIR, f"{slot_id}_final.mp4")

        build_seamless_loop(
            src_path, raw_dur, float(base.get("loop_crossfade_sec", 1.0)),
            raw_slot_path, width, height, fps
        )
        apply_inserts(
            raw_slot_path, slot.get("inserts", []), float(slot.get("start_sec", 0)),
            final_slot_path, width, height, fps, folder_id
        )
        built_slots.append((final_slot_path, slot_dur, cf_out))

    print(f"=== Финальный рендер в один проход: {out_filename} ({v_codec}, preset {preset}, CRF {crf}) ===")
    inputs_cmd = []
    for path, _, _ in built_slots:
        inputs_cmd.extend(["-i", path])

    filter_parts = []
    prev_v = "[0:v]"
    prev_a = "[0:a]"
    offset = built_slots[0][1]

    for idx in range(1, len(built_slots)):
        cf_dur = built_slots[idx - 1][2]
        out_v = f"[v{idx}]"
        out_a = f"[a{idx}]"
        if cf_dur > 0:
            filter_parts.append(f"{prev_v}[{idx}:v]xfade=transition=fade:duration={cf_dur}:offset={offset}{out_v}")
            filter_parts.append(f"{prev_a}[{idx}:a]acrossfade=d={cf_dur}{out_a}")
        else:
            filter_parts.append(f"{prev_v}[{idx}:v]concat=n=2:v=1:a=0{out_v}")
            filter_parts.append(f"{prev_a}[{idx}:a]concat=n=2:v=0:a=1{out_a}")
        prev_v = out_v
        prev_a = out_a
        offset += built_slots[idx][1]

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

    for path, _, _ in built_slots:
        safe_remove(path)

    with open("final_output_name.txt", "w") as f:
        f.write(out_filename)


if __name__ == "__main__":
    main()
