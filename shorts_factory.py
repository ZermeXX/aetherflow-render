import json
import os
import subprocess

MANIFEST_FILE = "manifest.json"
SHORTS_DIR = "shorts_out"
os.makedirs(SHORTS_DIR, exist_ok=True)


def run(cmd):
    print(f"RUN: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def get_duration(file_path):
    res = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", file_path
    ]).decode().strip()
    return float(res)


def main():
    if not os.path.exists("final_output_name.txt"):
        print("Нет final_output_name.txt — пропускаем генерацию Shorts.")
        return

    with open("final_output_name.txt", "r", encoding="utf-8") as f:
        master_video = f.read().strip()

    with open(MANIFEST_FILE, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    shorts_cfg = manifest.get("shorts_factory", {})
    if not shorts_cfg.get("enabled", False):
        print("Генерация Shorts отключена в манифесте.")
        return

    clip_len = float(shorts_cfg.get("clip_duration_sec", 38.0))
    total_dur = get_duration(master_video)
    base_name = os.path.splitext(master_video)[0]

    # Собираем точки интереса из врезок (inserts) или начала актов
    triggers = []
    for slot in manifest.get("timeline_slots", []):
        for ins in slot.get("inserts", []):
            t = float(ins.get("trigger_at_sec", 0))
            label = ins.get("label") or ins.get("file_name", "moment")
            triggers.append((max(0.0, t - 10.0), label))

    if not triggers:
        for idx, slot in enumerate(manifest.get("timeline_slots", [])[:3]):
            triggers.append((float(slot.get("start_sec", idx * 600)) + 15.0, f"act_{idx+1}"))

    generated = []
    for idx, (start_sec, label) in enumerate(triggers[:4]):
        if start_sec + clip_len > total_dur:
            start_sec = max(0.0, total_dur - clip_len)

        out_short = os.path.join(SHORTS_DIR, f"{base_name}_SHORT_{idx+1:02d}.mp4")
        # Кроп 4K (3840x2160) в 9:16 (1080x1920) с плавным горизонтальным дрейфом камеры
        vf_pan = (
            "scale=-2:1920,"
            "crop=1080:1920:x='(iw-1080)/2 + ((iw-1080)/2.5)*sin(2*PI*t/38)':y=0,"
            "fade=t=in:st=0:d=0.8,fade=t=out:st=" + f"{clip_len - 0.8:.2f}" + ":d=0.8"
        )
        af_fade = f"afade=t=in:st=0:d=0.8,afade=t=out:st={clip_len - 0.8:.2f}:d=0.8"

        run([
            "ffmpeg", "-y",
            "-ss", f"{start_sec:.2f}", "-t", f"{clip_len:.2f}",
            "-i", master_video,
            "-vf", vf_pan,
            "-af", af_fade,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "256k", "-ar", "48000",
            out_short
        ])
        generated.append(out_short)

    with open("generated_shorts.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(generated))
    print(f"✅ Сгенерировано {len(generated)} вертикальных Shorts в {SHORTS_DIR}/")


if __name__ == "__main__":
    main()
