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
    raise FileNotFoundError(f"Файл '{filename}' не найден в {input_dir}. Найдено файлов: {len(os.listdir(input_dir))}")

def main():
    manifest_path = sys.argv[1] if len(sys.argv) > 1 else 'current_manifest.json'
    if not os.path.exists(manifest_path):
        manifest_path = 'current_manifest.json'

    with open(manifest_path, 'r', encoding='utf-8') as f:
        manifest = json.load(f)

    input_dir = "./input_clips"
    output_dir = "./output"
    temp_dir = "./temp_render"
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(temp_dir, exist_ok=True)

    export_settings = manifest.get('export_settings', {})
    slots = manifest.get('timeline_slots', [])
    total_slots = len(slots)

    # Принудительно ставим скоростной libx264 veryfast (если явно не передан другой в манифесте)
    v_codec = export_settings.get('video_codec', 'libx264')
    preset = "veryfast" if v_codec == "libx264" else "ultrafast"
    crf = str(export_settings.get('crf', 20 if v_codec == 'libx264' else 22))
    
    out_filename = export_settings.get('output_filename', f"FINAL_RENDER_4K_{v_codec}.mp4")
    final_output = os.path.join(output_dir, out_filename)

    print(f"=== Старт пайплайна многочасового лупа 4K ({v_codec} / CRF {crf}) ===", flush=True)
    print(f"Всего слотов таймлайна: {total_slots}", flush=True)

    rendered_slot_files = []

    # 1. Обработка и бесшовное растягивание каждого слота с апскейлом до 4K
    for idx, slot in enumerate(slots):
        slot_id = slot.get('slot_id', f"slot_{idx+1}")
        base_name = slot.get('base_content', {}).get('file_name') or slot.get('file_name')
        duration = slot.get('duration_sec', 1800)  # по умолчанию 30 мин на слот
        slot_out = os.path.join(temp_dir, f"{slot_id}.mp4")

        base_file = resolve_file(input_dir, base_name)
        print(f"\n[{idx+1}/{total_slots}] Рендеринг слота {slot_id} ({duration} сек) из {base_name}...", flush=True)

        # Формируем зацикливание 10-секундного исходника на нужную длину слота в 4K
        cmd = [
            "ffmpeg", "-y",
            "-stream_loop", "-1",
            "-i", base_file,
            "-t", str(duration),
            "-vf", "scale=3840:2160:force_original_aspect_ratio=decrease,pad=3840:2160:(ow-iw)/2:(oh-ih)/2,fps=24",
            "-c:v", v_codec,
            "-preset", preset,
            "-crf", crf,
            "-c:a", "aac",
            "-b:a", "320k",
            slot_out
        ]
        run_cmd(cmd)
        rendered_slot_files.append(slot_out)

    # 2. Бесшовная склейка готовых слотов в финальный многочасовой мастер
    print("\n=== Объединение всех слотов в финальный 4K мастер-файл ===", flush=True)
    concat_list = os.path.join(temp_dir, "slots_concat.txt")
    with open(concat_list, "w", encoding="utf-8") as f:
        for sf in rendered_slot_files:
            abs_p = os.path.abspath(sf)
            f.write(f"file '{abs_p}'\n")

    run_cmd([
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_list,
        "-c", "copy",
        final_output
    ])

    # Очистка временных частей слотов на диске раннера
    for sf in rendered_slot_files:
        try:
            os.remove(sf)
        except OSError:
            pass

    size_gb = os.path.getsize(final_output) / (1024 * 1024 * 1024)
    print(f"\n[ГОТОВО]: Многочасовой 4K луп успешно собран: {final_output} ({size_gb:.2f} ГБ)", flush=True)

if __name__ == '__main__':
    main()
