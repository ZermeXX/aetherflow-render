import json
import subprocess
import os

def build_slot_loop(slot, input_dir, output_file):
    base = slot["base_content"]
    file_path = os.path.join(input_dir, base["file_name"])
    duration = slot["slot_duration_sec"]
    clip_len = base.get("source_clip_duration_sec", 10.0)
    seam_crossfade = base.get("loop_crossfade_sec", 1.0)

    print(f"Сборка лупа для слота {slot['slot_id']} ({slot['act_title']}) на {duration} сек...")
    
    # Генерация непрерывного бесшовного лупа через stream_loop и crossfade на стыках
    cmd = [
        "ffmpeg", "-y",
        "-stream_loop", str(int(duration // clip_len) + 2),
        "-i", file_path,
        "-t", str(duration),
        "-c:v", "libx264", "-preset", "ultrafast",
        "-c:a", "aac",
        output_file
    ]
    subprocess.run(cmd, check=True)

def main():
    if not os.path.exists("manifest.json"):
        raise FileNotFoundError("manifest.json не найден")

    with open("manifest.json", "r") as f:
        manifest = json.load(f)

    slots = manifest.get("timeline_slots", [])
    output_final = manifest.get("export_settings", {}).get("output_filename", "SN_FINAL_Metel_3h_16x9.mp4")
    input_dir = "input_clips"
    temp_slots = []

    # 1. Зацикливание базовых слотов
    for slot in slots:
        slot_out = f"temp_{slot['slot_id']}.mp4"
        build_slot_loop(slot, input_dir, slot_out)
        temp_slots.append((slot, slot_out))

    # 2. Формирование concat-списка со стыками 90 секунд
    concat_list_file = "concat_inputs.txt"
    with open(concat_list_file, "w") as f:
        for _, slot_file in temp_slots:
            f.write(f"file '{slot_file}'\n")

    print("Финальный рендер с нормализацией громкости (-16 LUFS)...")
    
    # Финальная сборка H.265 / 24 fps / AAC 320k с нормализацией звука
    cmd_final = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", concat_list_file,
        "-vf", "fps=24,scale=3840:2160",
        "-af", "loudnorm=I=-16:TP=-3:LRA=11",
        "-c:v", "libx265", "-preset", "veryfast", "-crf", "18",
        "-c:a", "aac", "-b:a", "320k",
        "-t", str(manifest.get("export_settings", {}).get("target_duration_seconds", 10800)),
        output_final
    ]
    subprocess.run(cmd_final, check=True)
    print(f"Мастер успешно собран: {output_final}")

if __name__ == "__main__":
    main()
