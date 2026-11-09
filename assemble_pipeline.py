import sys
import json
import os
import subprocess

def run_cmd(cmd):
    print(f"\n[EXEC]: {' '.join(cmd) if isinstance(cmd, list) else cmd}", flush=True)
    res = subprocess.run(cmd, shell=isinstance(cmd, str), check=True)
    return res

def resolve_file(input_dir, filename):
    if not filename:
        raise ValueError("Имя файла не указано в манифесте!")
    exact_path = os.path.join(input_dir, filename)
    if os.path.exists(exact_path):
        return exact_path
    for f in os.listdir(input_dir):
        if f.lower() == filename.lower():
            return os.path.join(input_dir, f)
    raise FileNotFoundError(f"Файл '{filename}' не найден в {input_dir}. Найдено: {os.listdir(input_dir)}")

def main():
    manifest_path = sys.argv[1] if len(sys.argv) > 1 else 'current_manifest.json'
    if not os.path.exists(manifest_path):
        manifest_path = 'assembly_manifest_metel_3h.json'

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

    v_codec = export_settings.get('video_codec', 'libx265')
    crf = str(export_settings.get('crf', 22))
    a_codec = export_settings.get('audio_codec', 'aac')
    a_bitrate = export_settings.get('audio_bitrate', '320k')

    print(f"=== Запуск сборки проекта: {manifest.get('project_name', 'Unnamed')} ===", flush=True)
    print(f"Слотов к обработке: {len(slots)}", flush=True)

    rendered_slots = []

    # 1. Сборка каждого слота (зацикливание исходника на длительность слота)
    for idx, slot in enumerate(slots):
        slot_id = slot.get('slot_id', f'slot_{idx+1}')
        base_name = slot.get('base_content', {}).get('file_name') or slot.get('file_name')
        duration = int(slot.get('slot_duration_sec', 1800))
        slot_out = os.path.join(temp_dir, f"{slot_id}.mp4")

        input_clip = resolve_file(input_dir, base_name)
        clip_dur = float(slot.get('base_content', {}).get('source_clip_duration_sec', 10.0))
        loop_count = max(1, int(duration / clip_dur) + 1)

        print(f"\n--- Сборка слота {idx+1}/{len(slots)}: {slot.get('act_title', slot_id)} ---", flush=True)
        print(f"Клип: {base_name} | Длительность: {duration}с | Повторов: {loop_count}", flush=True)

        cmd = [
            "ffmpeg", "-y",
            "-stream_loop", str(loop_count),
            "-i", input_clip,
            "-t", str(duration),
            "-c:v", v_codec,
            "-preset", "ultrafast",
            "-crf", crf,
            "-c:a", a_codec,
            "-b:a", a_bitrate,
            slot_out
        ]
        run_cmd(cmd)
        rendered_slots.append(slot_out)

    # 2. Объединение слотов в финальное видео
    concat_list = os.path.join(temp_dir, "concat.txt")
    with open(concat_list, "w", encoding="utf-8") as f:
        for s in rendered_slots:
            abs_path = os.path.abspath(s)
            f.write(f"file '{abs_path}'\n")

    print(f"\n=== Объединение всех слотов в {final_output} ===", flush=True)
    concat_cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_list,
        "-c", "copy",
        final_output
    ]
    run_cmd(concat_cmd)

    file_size_mb = os.path.getsize(final_output) / (1024 * 1024)
    print(f"\n[ГОТОВО]: Финальный файл создан: {final_output} ({file_size_mb:.2f} МБ)", flush=True)

if __name__ == '__main__':
    main()
