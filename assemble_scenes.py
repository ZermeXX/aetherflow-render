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
    manifest_path = sys.argv[1] if len(sys.argv) > 1 else 'narrative_manifest.json'
    with open(manifest_path, 'r', encoding='utf-8') as f:
        manifest = json.load(f)

    input_dir = "./input_scenes"
    output_dir = "./output"
    temp_dir = "./temp_scenes"
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(temp_dir, exist_ok=True)

    scenes = manifest.get('scenes', [])
    audio_track = manifest.get('master_audio') or manifest.get('audio_track', {}).get('file_name')
    export_settings = manifest.get('export_settings', {})
    out_filename = export_settings.get('output_filename', 'FINAL_NARRATIVE_60_SCENES.mp4')
    final_output = os.path.join(output_dir, out_filename)

    v_codec = export_settings.get('video_codec', 'libx265')
    crf = str(export_settings.get('crf', 22))

    print(f"=== Старт монтажа: {manifest.get('project_name', 'Narrative Movie')} ===", flush=True)
    print(f"Всего сцен к монтажу: {len(scenes)}", flush=True)

    rendered_scenes = []

    # 1. Подготовка и нормализация каждого фрагмента
    for idx, sc in enumerate(scenes):
        scene_id = sc.get('scene_id', f'scene_{idx+1}')
        fname = sc.get('file_name')
        dur = sc.get('duration_sec') or sc.get('duration')
        scene_out = os.path.join(temp_dir, f"{scene_id}.mp4")

        in_file = resolve_file(input_dir, fname)
        print(f"[{idx+1}/{len(scenes)}] Подготовка сцены {scene_id} -> {fname}", flush=True)

        cmd = ["ffmpeg", "-y", "-i", in_file]
        if dur:
            cmd.extend(["-t", str(dur)])
        
        # Приводим к единому разрешению и частоте кадров для идеальной склейки
        cmd.extend([
            "-vf", "scale=3840:2160:force_original_aspect_ratio=decrease,pad=3840:2160:(ow-iw)/2:(oh-ih)/2,fps=24",
            "-c:v", v_codec,
            "-preset", "ultrafast",
            "-crf", crf,
            "-an",
            scene_out
        ])
        run_cmd(cmd)
        rendered_scenes.append(scene_out)

    # 2. Создание списка конкатенации
    concat_list = os.path.join(temp_dir, "scenes_concat.txt")
    with open(concat_list, "w", encoding="utf-8") as f:
        for s in rendered_scenes:
            abs_p = os.path.abspath(s)
            f.write(f"file '{abs_p}'\n")

    video_only = os.path.join(temp_dir, "all_scenes_no_audio.mp4")
    print("\n=== Склейка всех сцен в единый видеопоток ===", flush=True)
    run_cmd(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list, "-c", "copy", video_only])

    # 3. Наложение мастер-аудиодорожки
    if audio_track:
        audio_file = resolve_file(input_dir, audio_track)
        print(f"\n=== Микширование с мастер-аудио: {audio_track} ===", flush=True)
        run_cmd([
            "ffmpeg", "-y",
            "-i", video_only,
            "-i", audio_file,
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "320k",
            "-shortest",
            final_output
        ])
    else:
        print("\n=== Аудиодорожка не указана, сохранение только видеоряда ===", flush=True)
        os.rename(video_only, final_output)

    print(f"\n[ГОТОВО]: Фильм собран: {final_output} ({os.path.getsize(final_output) / (1024*1024):.2f} МБ)", flush=True)

if __name__ == '__main__':
    main()
