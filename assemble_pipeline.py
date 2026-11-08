import json
import os
import subprocess
import sys

def get_slot_base_clip(slot):
    """Извлекает имя файла и параметры клипа из любой версии манифеста."""
    # 1. Формат с вложенным объектом base_content
    if "base_content" in slot and isinstance(slot["base_content"], dict):
        base = slot["base_content"]
        return {
            "file_name": base.get("file_name") or base.get("file"),
            "clip_len": float(base.get("source_clip_duration_sec", 10.0)),
            "loop_crossfade": float(base.get("loop_crossfade_sec", 1.0))
        }
    
    # 2. Упрощенный/плоский формат манифеста
    file_name = slot.get("file_name") or slot.get("file") or slot.get("clip")
    if file_name:
        return {
            "file_name": file_name,
            "clip_len": float(slot.get("source_clip_duration_sec", 10.0)),
            "loop_crossfade": float(slot.get("loop_crossfade_sec", 1.0))
        }

    return None

def build_slot_loop(slot, input_dir, output_file):
    base_info = get_slot_base_clip(slot)
    
    if not base_info or not base_info["file_name"]:
        raise ValueError(f"Слот {slot.get('slot_id', 'unknown')} не содержит имени файла клипа! Проверьте структуру JSON.")

    file_path = os.path.join(input_dir, base_info["file_name"])
    duration = slot.get("slot_duration_sec")
    
    if not duration:
        start = slot.get("start_sec", 0)
        end = slot.get("end_sec", 0)
        duration = end - start if end > start else 1800

    clip_len = base_info["clip_len"]
    loop_count = int(duration // clip_len) + 2

    print(f"\n--- Сборка слота: {slot.get('slot_id')} ({slot.get('act_title', 'Без названия')}) ---")
    print(f"Файл: {base_info['file_name']}, целевая длительность: {duration} сек, циклов: {loop_count}")

    # Проверка физического наличия клипа на диске
    if not os.path.exists(file_path):
        print(f"Внимание: файл {file_path} не найден в {input_dir}. Поиск по маске...")
        available = os.listdir(input_dir) if os.path.exists(input_dir) else []
        print(f"Доступные файлы в папке: {available}")
        if available:
            file_path = os.path.join(input_dir, available[0])
            print(f"Используем запасной файл: {file_path}")
        else:
            raise FileNotFoundError(f"В папке {input_dir} нет видеофайлов для рендера!")

    cmd = [
        "ffmpeg", "-y",
        "-stream_loop", str(loop_count),
        "-i", file_path,
        "-t", str(duration),
        "-c:v", "libx264", "-preset", "ultrafast",
        "-c:a", "aac",
        output_file
    ]
    subprocess.run(cmd, check=True)

def main():
    if not os.path.exists("manifest.json"):
        print("Ошибка: manifest.json отсутствует!")
        sys.exit(1)

    with open("manifest.json", "r", encoding="utf-8") as f:
        try:
            manifest = json.load(f)
        except Exception as e:
            print(f"Ошибка парсинга manifest.json: {e}")
            sys.exit(1)

    slots = manifest.get("timeline_slots", [])
    if not slots:
        print("Ошибка: массив timeline_slots пуст или отсутствует в манифесте!")
        sys.exit(1)

    output_final = manifest.get("export_settings", {}).get("output_filename", "SN_FINAL_Metel_3h_16x9.mp4")
    input_dir = "input_clips"
    temp_slots = []

    os.makedirs(input_dir, exist_ok=True)

    # 1. Зацикливание слотов
    for i, slot in enumerate(slots):
        slot_id = slot.get("slot_id", f"slot_{i+1}")
        slot_out = f"temp_{slot_id}.mp4"
        build_slot_loop(slot, input_dir, slot_out)
        temp_slots.append(slot_out)

    # 2. Создание concat-файла
    concat_list_file = "concat_inputs.txt"
    with open(concat_list_file, "w", encoding="utf-8") as f:
        for slot_file in temp_slots:
            f.write(f"file '{slot_file}'\n")

    print("\n--- Финальная сборка H.265 / 24 fps / AAC 320k ---")
    target_duration = manifest.get("export_settings", {}).get("target_duration_seconds", 10800)

    cmd_final = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", concat_list_file,
        "-vf", "fps=24,scale=3840:2160",
        "-af", "loudnorm=I=-16:TP=-3:LRA=11",
        "-c:v", "libx265", "-preset", "veryfast", "-crf", "18",
        "-c:a", "aac", "-b:a", "320k",
        "-t", str(target_duration),
        output_final
    ]
    subprocess.run(cmd_final, check=True)
    print(f"\n[Успех] Итоговый файл готов: {output_final}")

if __name__ == "__main__":
    main()
