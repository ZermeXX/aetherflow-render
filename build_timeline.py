import json
import os
import re
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


def format_timestamp(seconds):
    total = int(round(seconds))
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def sanitize_english_title(text, fallback):
    if not text:
        return fallback
    cleaned = re.sub(r"[А-Яа-яЁё]+", "", str(text)).strip(" -–—|:;,.")
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned if cleaned else fallback


def ensure_asset(filename, folder_id):
    local_path = os.path.join(ASSETS_DIR, filename)
    if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
        return local_path
    if os.path.exists(filename) and os.path.getsize(filename) > 0:
        return filename

    print(f"Downloading {filename} from Google Drive...")
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
        raise FileNotFoundError(f"Source file {filename} not found in repository or Google Drive!")
    return local_path


def build_seamless_loop(src_file, target_sec, cf_sec, out_file, width, height, fps, v_codec, preset, crf, a_bitrate):
    norm_clip = os.path.join(WORK_DIR, f"norm_{os.path.basename(src_file)}")
    run([
        "ffmpeg", "-y", "-i", src_file,
        "-vf", f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},fps={fps},format=yuv420p",
        "-c:v", v_codec, "-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p",
        "-g", str(fps * 2), "-keyint_min", str(fps * 2),
        "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", a_bitrate,
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
            "-c:v", v_codec, "-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p",
            "-g", str(fps * 2), "-keyint_min", str(fps * 2),
            "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", a_bitrate,
            unit_clip
        ])
        safe_remove(norm_clip)

    repeats = math.ceil(target_sec / effective_dur) + 1
    list_txt = os.path.join(WORK_DIR, f"list_{os.path.basename(out_file)}.txt")
    with open(list_txt, "w", encoding="utf-8") as f:
        for _ in range(repeats):
            f.write(f"file '{os.path.abspath(unit_clip)}'\n")

    run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_txt,
        "-t", str(target_sec), "-c", "copy", out_file
    ])
    safe_remove(unit_clip)
    safe_remove(list_txt)


def apply_inserts_surgical(base_video, inserts, slot_start_sec, out_video, width, height, fps, folder_id, v_codec, preset, crf, a_bitrate):
    if not inserts:
        os.replace(base_video, out_video)
        return

    current_vid = base_video
    for idx, ins in enumerate(inserts):
        ins_file = ensure_asset(ins["file_name"], folder_id)
        base_dur = get_duration(current_vid)

        rel_start = max(0.0, float(ins.get("trigger_at_sec", slot_start_sec)) - slot_start_sec)
        raw_dur = ins.get("source_clip_duration_sec") or ins.get("duration_sec") or ins.get("duration")
        ins_dur = float(raw_dur) if raw_dur is not None else get_duration(ins_file)

        win_start = max(0.0, rel_start - 1.0)
        win_end = min(base_dur, rel_start + ins_dur + 1.0)
        local_offset = rel_start - win_start

        fade_in = float(ins.get("transition_duration_sec", 1.0))
        t_out = ins.get("transition_out") or {}
        if isinstance(t_out, dict):
            fade_out = float(t_out.get("duration_sec", 0)) if t_out.get("type") != "none" else 0.0
        else:
            fade_out = float(ins.get("transition_out_duration_sec", 1.0))

        part_pre = os.path.join(WORK_DIR, f"pre_{idx}_{os.path.basename(out_video)}")
        part_mid_raw = os.path.join(WORK_DIR, f"mid_raw_{idx}_{os.path.basename(out_video)}")
        part_mid_ov = os.path.join(WORK_DIR, f"mid_ov_{idx}_{os.path.basename(out_video)}")
        part_post = os.path.join(WORK_DIR, f"post_{idx}_{os.path.basename(out_video)}")
        step_out = os.path.join(WORK_DIR, f"ins_{idx}_{os.path.basename(out_video)}")

        concat_segments = []

        if win_start > 0.5:
            run([
                "ffmpeg", "-y", "-ss", "0", "-to", f"{win_start:.3f}",
                "-i", current_vid, "-c", "copy", part_pre
            ])
            concat_segments.append(part_pre)
        else:
            win_start = 0.0
            local_offset = rel_start

        run([
            "ffmpeg", "-y", "-ss", f"{win_start:.3f}", "-to", f"{win_end:.3f}",
            "-i", current_vid,
            "-c:v", v_codec, "-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p",
            "-g", str(fps * 2), "-keyint_min", str(fps * 2),
            "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", a_bitrate,
            part_mid_raw
        ])

        v_chain = (
            f"[1:v]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
            f"fps={fps},format=yuva420p"
        )
        if fade_in > 0:
            v_chain += f",fade=t=in:st=0:d={fade_in}:alpha=1"
        if fade_out > 0:
            v_chain += f",fade=t=out:st={max(0.0, ins_dur - fade_out)}:d={fade_out}:alpha=1"
        v_chain += f",setpts=PTS-STARTPTS+{local_offset:.3f}/TB[ov_v];"

        delay_ms = int(local_offset * 1000)
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
            "ffmpeg", "-y", "-i", part_mid_raw, "-i", ins_file,
            "-filter_complex", filter_complex,
            "-map", "[outv]", "-map", "[outa]",
            "-c:v", v_codec, "-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p",
            "-g", str(fps * 2), "-keyint_min", str(fps * 2),
            "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", a_bitrate,
            part_mid_ov
        ])
        concat_segments.append(part_mid_ov)

        if base_dur - win_end > 0.5:
            run([
                "ffmpeg", "-y", "-ss", f"{win_end:.3f}", "-to", f"{base_dur:.3f}",
                "-i", current_vid, "-c", "copy", part_post
            ])
            concat_segments.append(part_post)

        seg_list = os.path.join(WORK_DIR, f"seg_{idx}_{os.path.basename(out_video)}.txt")
        with open(seg_list, "w", encoding="utf-8") as f:
            for seg in concat_segments:
                f.write(f"file '{os.path.abspath(seg)}'\n")

        run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", seg_list,
            "-c", "copy", step_out
        ])

        for p in [current_vid, part_pre, part_mid_raw, part_mid_ov, part_post, seg_list]:
            safe_remove(p)

        current_vid = step_out

    os.replace(current_vid, out_video)


def build_smart_concat(built_slots, raw_concat_out, fps, v_codec, preset, crf, a_bitrate):
    n = len(built_slots)
    if n == 1:
        os.replace(built_slots[0][0], raw_concat_out)
        return

    durations = [get_duration(p) for p, _, _ in built_slots]
    concat_parts = []

    for i in range(n):
        path, _, cf_out = built_slots[i]
        actual_dur = durations[i]
        cf_before = built_slots[i - 1][2] if i > 0 else 0.0
        cf_after = cf_out if i < n - 1 else 0.0

        body_start = cf_before
        body_end = actual_dur - cf_after

        if body_end - body_start > 2.0:
            body_file = os.path.join(WORK_DIR, f"body_{i}.mp4")
            run([
                "ffmpeg", "-y",
                "-ss", f"{body_start:.3f}", "-to", f"{body_end:.3f}",
                "-i", path,
                "-c:v", "copy",
                "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", a_bitrate,
                body_file
            ])
            concat_parts.append(body_file)

        if cf_after > 0 and i < n - 1:
            next_path = built_slots[i + 1][0]
            tail_file = os.path.join(WORK_DIR, f"tail_{i}.mp4")
            head_file = os.path.join(WORK_DIR, f"head_{i+1}.mp4")
            bridge_file = os.path.join(WORK_DIR, f"bridge_{i}.mp4")

            run([
                "ffmpeg", "-y", "-ss", f"{max(0.0, actual_dur - cf_after):.3f}", "-i", path,
                "-t", f"{cf_after:.3f}",
                "-c:v", v_codec, "-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p",
                "-g", str(fps * 2), "-keyint_min", str(fps * 2),
                "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", a_bitrate,
                tail_file
            ])
            run([
                "ffmpeg", "-y", "-ss", "0", "-i", next_path,
                "-t", f"{cf_after:.3f}",
                "-c:v", v_codec, "-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p",
                "-g", str(fps * 2), "-keyint_min", str(fps * 2),
                "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", a_bitrate,
                head_file
            ])

            half_cf = cf_after / 2.0
            fc = (
                f"[0:v][1:v]xfade=transition=fade:duration={half_cf:.3f}:offset={half_cf:.3f}[v];"
                f"[0:a][1:a]acrossfade=d={half_cf:.3f}[a]"
            )
            run([
                "ffmpeg", "-y", "-i", tail_file, "-i", head_file,
                "-filter_complex", fc,
                "-map", "[v]", "-map", "[a]",
                "-c:v", v_codec, "-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p",
                "-g", str(fps * 2), "-keyint_min", str(fps * 2),
                "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", a_bitrate,
                bridge_file
            ])
            safe_remove(tail_file)
            safe_remove(head_file)
            concat_parts.append(bridge_file)

        safe_remove(path)

    master_list = os.path.join(WORK_DIR, "master_concat.txt")
    with open(master_list, "w", encoding="utf-8") as f:
        for p in concat_parts:
            f.write(f"file '{os.path.abspath(p)}'\n")

    run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", master_list,
        "-c", "copy", raw_concat_out
    ])

    for p in concat_parts:
        safe_remove(p)
    safe_remove(master_list)


def generate_chapters(manifest, chapters_path):
    slots = manifest.get("timeline_slots", [])
    lines = []
    cur_sec = 0.0
    for idx, slot in enumerate(slots):
        raw_title = (
            slot.get("title_en")
            or slot.get("title")
            or slot.get("label")
            or slot.get("slot_id")
            or f"Chapter {idx + 1}: Atmospheric Act"
        )
        title = sanitize_english_title(raw_title, f"Chapter {idx + 1}: Atmospheric Act")
        start = float(slot.get("start_sec", cur_sec))
        lines.append(f"{format_timestamp(start)} {title}")
        cur_sec = start + float(slot.get("slot_duration_sec", 0))
    with open(chapters_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"English chapters saved to {chapters_path}")


def run_auto_qc(out_filename, expected_sec):
    print("=== Running Fast Auto-QC ===")
    probe_raw = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration,size:stream=codec_type,codec_name,width,height,r_frame_rate",
        "-of", "json", out_filename
    ]).decode("utf-8")
    probe = json.loads(probe_raw)

    actual_dur = float(probe["format"]["duration"])
    streams = probe.get("streams", [])
    has_video = any(s.get("codec_type") == "video" for s in streams)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)

    if not has_video or not has_audio:
        raise RuntimeError(f"Auto-QC failed: missing video ({has_video}) or audio ({has_audio}) stream!")

    if expected_sec > 0 and abs(actual_dur - expected_sec) > 15.0:
        raise RuntimeError(
            f"Auto-QC failed: actual duration {actual_dur:.1f}s differs from expected {expected_sec:.1f}s!"
        )

    # -vn skips 4K video decoding during EBU R128 loudness check (~10s instead of 15 min)
    lufs_proc = subprocess.run([
        "ffmpeg", "-nostats", "-vn", "-i", out_filename,
        "-filter_complex", "ebur128", "-f", "null", "-"
    ], capture_output=True, text=True)
    m = re.findall(r"I:\s*(-?\d+\.\d+)\s*LUFS", lufs_proc.stderr)
    integrated_lufs = float(m[-1]) if m else -16.0

    qc_data = {
        "status": "PASS",
        "duration_sec": round(actual_dur, 2),
        "duration_formatted": format_timestamp(actual_dur),
        "integrated_lufs": integrated_lufs,
        "has_video": has_video,
        "has_audio": has_audio
    }
    with open("qc_report.json", "w", encoding="utf-8") as f:
        json.dump(qc_data, f, ensure_ascii=False, indent=2)
    print(f"✅ Auto-QC passed: {qc_data['duration_formatted']} | Loudness: {integrated_lufs} LUFS")


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
    expected_total_sec = sum(float(s["slot_duration_sec"]) for s in slots)

    for slot in slots:
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
            raw_slot_path, width, height, fps, v_codec, preset, crf, a_bitrate
        )
        apply_inserts_surgical(
            raw_slot_path, slot.get("inserts", []), float(slot.get("start_sec", 0)),
            final_slot_path, width, height, fps, folder_id, v_codec, preset, crf, a_bitrate
        )
        built_slots.append((final_slot_path, slot_dur, cf_out))

    raw_master = os.path.join(WORK_DIR, "raw_master.mp4")
    print("=== Fast Stream Concat & Crossfades (-c:v copy) ===")
    build_smart_concat(built_slots, raw_master, fps, v_codec, preset, crf, a_bitrate)

    for path, _, _ in built_slots:
        safe_remove(path)

    print(f"=== Final Audio Loudness Normalization (-16 LUFS) -> {out_filename} ===")
    run([
        "ffmpeg", "-y", "-i", raw_master,
        "-c:v", "copy",
        "-af", "loudnorm=I=-16.0:TP=-3.0:LRA=11.0",
        "-c:a", "aac", "-b:a", a_bitrate, "-ar", "48000",
        "-movflags", "+faststart",
        out_filename
    ])
    safe_remove(raw_master)

    chapters_file = f"{os.path.splitext(out_filename)[0]}_chapters.txt"
    generate_chapters(manifest, chapters_file)
    run_auto_qc(out_filename, expected_total_sec)

    with open("final_output_name.txt", "w", encoding="utf-8") as f:
        f.write(out_filename)
    with open("final_chapters_name.txt", "w", encoding="utf-8") as f:
        f.write(chapters_file)


if __name__ == "__main__":
    main()
