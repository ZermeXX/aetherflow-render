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
    if not os.path.exists(manifest_path):
        manifest_path = 'current_manifest.json'

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
    out_filename = export_settings.get('output_filename', 'FINAL_NARRATIVE_4K.mp4')
    final_output = os.path.join(output_dir, out_filename)

    v_codec = export_settings.get('video_codec', 'libx265')
    crf = str(export_settings.get('crf', 22))

    print(f"=== Старт 4K монтажа сцен: {manifest.get('project_name', 'Narrative Movie')} ===", flush=True)
    print(f"Всего сцен к обработке: {len(scenes)}", flush=True)

    rendered_scenes = []

    # 1. Приведение каждой сцены к строгому 4K 24fps (апскейл 720p/1080p -> 3840x2160)
    for idx, sc in enumerate(scenes):
        scene_id = sc.get('scene_id', f'scene_{idx+1}')
        fname = sc.get('file_name')
        dur = sc.get('duration_sec') or sc.get('duration')
        scene_out = os.path.join(temp_dir, f"{scene_id}.mp4")

        in_file = resolve_file(input_dir, fname)
        print(f"[{idx+1}/{len(scenes)}] Апскейл в 4K для сцены {scene_id} -> {fname}", flush=True)

        cmd = ["ffmpeg", "-y", "-i", in_file]
        if dur:
            cmd.extend(["-t", str(dur)])
        
        # Гарантированное масштабирование любого исходника в 3840x2160 с центрированием и 24fps
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

    # 2. Формирование concat-листа для мгновенной склейки без повторного пережатия
    concat_list = os.path.join(temp_dir, "scenes_concat.txt")
    with open(concat_list, "w", encoding="utf-8") as f:
        for s in rendered_scenes:
            abs_p = os.path.abspath(s)
            f.write(f"file '{abs_p}'\n")

    video_only = os.path.join(temp_dir, "all_scenes_4k_no_audio.mp4")
    print("\n=== Быстрая склейка всех 4K-сцен в единый видеоряд ===", flush=True)
    run_cmd([
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_list,
        "-c", "copy",
        video_only
    ])

    # 3. Финальное сведение с общей аудиодорожкой
    if audio_track:
        audio_file = resolve_file(input_dir, audio_track)
        print(f"\n=== Наложение мастер-аудио: {audio_track} ===", flush=True)
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
        print("\n=== Аудиодорожка не передана, финальный 4K файл сформирован без звука ===", flush=True)
        if os.path.exists(final_output):
            os.remove(final_output)
        os.rename(video_only, final_output)

    file_size_mb = os.path.getsize(final_output) / (1024 * 1024)
    print(f"\n[ГОТОВО]: Финальный 4K фильм успешно создан: {final_output} ({file_size_mb:.2f} МБ)", flush=True)

if __name__ == '__main__':
    main()
