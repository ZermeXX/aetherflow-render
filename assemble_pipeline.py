import sys
import json
import os
import subprocess

def main():
    manifest_file = sys.argv[1] if len(sys.argv) > 1 else 'assembly_manifest_metel_3h.json'
    if not os.path.exists(manifest_file):
        manifest_file = 'manifest.json'

    with open(manifest_file, 'r', encoding='utf-8') as f:
        manifest = json.load(f)

    input_dir = "./input_clips"
    output_dir = "./output"
    os.makedirs(output_dir, exist_ok=True)

    available_files = os.listdir(input_dir) if os.path.exists(input_dir) else []
    print(f"--- Запуск сборки проекта: {manifest.get('project_name', 'Unnamed')} ---")
    print(f"Файлы в input_clips: {available_files}")

    slots = manifest.get('timeline_slots', [])
    for slot in slots:
        base_name = slot.get('base_content', {}).get('file_name')
        print(f"Обработка слота: {slot.get('slot_id')} -> файл {base_name}")
        
        file_path = os.path.join(input_dir, base_name)
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Файл {base_name} не найден в {input_dir}. Доступны: {available_files}")

    # Вызов генерации FFmpeg по манифесту...
    print("Все входные файлы на месте. Запуск рендеринга FFmpeg...")

if __name__ == '__main__':
    main()
