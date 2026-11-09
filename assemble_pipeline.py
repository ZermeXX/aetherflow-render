import sys
import json
import os
import subprocess

def run_cmd(cmd):
    print(f"\n[RUN]: {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    res = subprocess.run(cmd, shell=isinstance(cmd, str), check=True)
    return res

def get_file(input_dir, filename):
    p = os.path.join(input_dir, filename)
    if os.path.exists(p):
        return p
    # Поиск без учета регистра или совпадения префикса
    for f in os.listdir(input_dir):
        if f.lower() == filename.lower():
            return os.path.join(input_dir, f)
    raise FileNotFoundError(f"Файл {filename} не найден в {input_dir}. Найдено: {os.listdir(input_dir)}")

def main():
    manifest_path = sys.argv[1] if len(sys.argv) > 1 else 'current_manifest.json'
    with open(manifest_path, 'r', encoding='utf-8') as f:
        manifest = json.load(f)

    input_dir = "./input_clips"
    output_dir = "./output"
    temp_dir = "./temp_render"
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(temp_dir, exist_ok=True)

    slots = manifest.get('timeline_slots', [])
    export_settings = manifest.get('export_settings', {})
    out_filename = export_settings.get('output_filename', 'FINAL_RENDER_3H.mp4')
    final_output = os.path.join(output_dir, out_filename)

    print(f"=== Старт сборки проекта: {manifest.get('project_name')} ===")
    print(f"Слотов для сборки: {len(slots)}")

    rendered_slots = []

    # 1. Сборка каждого слота (бесшовный луп на заданную длительность)
    for idx, slot in enumerate(slots):
        slot_id = slot.get('slot_id', f'slot_{idx+1}')
        file_name = slot.get('base_content', {}).get('file_name')
        duration = int(slot.get('slot_duration_sec', 1800))
        slot_out = os.path.join(temp_dir, f"{slot_id}.mp4")

        input_clip = get_file(input_dir, file_name)
        clip_dur = float(slot.get('base_content', {}).get('source_clip_duration_sec', 10.0))
        loop_count = max(1, int(duration / clip_dur) + 1)

        print(f"\n[Слот {idx+1}/{len(slots)}] {slot.get('act_title')} -> {file_name} (длительность: {duration}с, циклов: {loop_count})")

        # Создаем луп нужной длительности с подгонкой под таймлайн
        cmd = [
            "ffmpeg", "-y",
            "-stream_loop", str(loop_count),
            "-i", input_clip,
            "-t", str(duration),
            "-c:v", "libx265", "-preset", "ultrafast", "-crf", "22",
            "-c:a", "aac", "-b:a", "320k",
            slot_out
        ]
        run_cmd(cmd)
        rendered_slots.append(slot_out)

    # 2. Склейка слотов через concat
    concat_list = os.path.join(temp_dir, "concat.txt")
    with open(concat_list, "w", encoding="utf-8") as f:
        for s in rendered_slots:
            f.write(f"file '{os.path.abspath(s)}'\n")

    print(f"\n=== Финальное объединение в {final_output} ===")
    concat_cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", concat_list,
        "-c", "copy",
        final_output
    ]
    run_cmd(concat_cmd)
    print(f"\n Рендеринг завершен! Итоговый файл: {final_output} ({os.path.getsize(final_output) / (1024*1024):.2f} МБ)")

if __name__ == '__main__':
    main()
