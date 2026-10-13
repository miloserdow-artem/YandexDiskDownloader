import os
import time
import hashlib
import json
from pathlib import Path
from typing import Dict, List, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("YANDEX_DISK_TOKEN")
if not TOKEN:
    raise ValueError("Не найден YANDEX_DISK_TOKEN в .env")

BASE_URL = "https://cloud-api.yandex.net/v1/disk/resources"
CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB
MAX_RETRIES = 5
STATE_FILE = "yd_state.json"


def get_direct_link(path: str) -> str:
    """Получить свежую прямую ссылку для скачивания файла."""
    resp = requests.get(
        BASE_URL + "/download",
        params={"path": path},
        headers={"Authorization": f"OAuth {TOKEN}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["href"]


def md5_file(filepath: Path) -> str:
    hash_md5 = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()


def download_file(path_on_disk: str, dest: Path, expected_size: Optional[int] = None, expected_md5: Optional[str] = None) -> bool:
    """Скачать файл с получением свежей ссылки при каждой попытке, с проверкой размера и MD5."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            # Получаем свежую ссылку (актуально при ретраях, так как старые экспарятся)
            url = get_direct_link(path_on_disk)

            # (connect_timeout, read_timeout)
            with requests.get(url, stream=True, timeout=(10, 60)) as r:
                r.raise_for_status()
                total_downloaded = 0
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                        if not chunk:
                            continue
                        f.write(chunk)
                        total_downloaded += len(chunk)

            # Проверка размера
            actual_size = dest.stat().st_size
            if expected_size is not None and actual_size != expected_size:
                print(f"⚠️ Размер не совпадает: ожидалось {expected_size}, скачано {actual_size}. Попытка {attempt}/{MAX_RETRIES}")
                dest.unlink(missing_ok=True)
                time.sleep(2 ** attempt)
                continue

            # Проверка MD5 (если предоставлен Яндексом)
            if expected_md5 is not None:
                actual_md5 = md5_file(dest)
                if actual_md5 != expected_md5:
                    print(f"⚠️ MD5 не совпадает. Попытка {attempt}/{MAX_RETRIES}")
                    dest.unlink(missing_ok=True)
                    time.sleep(2 ** attempt)
                    continue

            print(f"✅ Файл успешно сохранён: {dest.name} ({actual_size} байт)")
            return True

        except Exception as e:
            print(f"❌ Ошибка при скачивании {dest.name} (попытка {attempt}/{MAX_RETRIES}): {e}")
            if dest.exists():
                dest.unlink(missing_ok=True)
            if attempt == MAX_RETRIES:
                print("❌ Превышено число попыток для этого файла.")
                return False
            time.sleep(2 ** attempt)

    return False


def list_folder(path: str) -> List[Dict]:
    """Рекурсивно получить список ВСЕХ файлов в папке Диска с учетом пагинации."""
    items = []
    limit = 500  # Максимальное количество элементов за один запрос в API Диска
    offset = 0

    while True:
        resp = requests.get(
            BASE_URL,
            params={"path": path, "limit": limit, "offset": offset},
            headers={"Authorization": f"OAuth {TOKEN}"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        embedded = data.get("_embedded", {})
        file_items = embedded.get("items", [])

        if not file_items:
            break

        for item in file_items:
            if item["type"] == "file":
                items.append({
                    "path": item["path"],
                    "name": item["name"],
                    "size": item.get("size"),
                    "md5": item.get("md5"),
                })
            elif item["type"] == "dir":
                # Рекурсивно заходим в подпапку
                items.extend(list_folder(item["path"]))

        offset += len(file_items)
        total = embedded.get("total", offset)
        if offset >= total:
            break

    return items


def load_state() -> Dict:
    if not os.path.exists(STATE_FILE):
        return {"completed": set(), "failed": set()}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
        return {
            "completed": set(data.get("completed", [])),
            "failed": set(data.get("failed", [])),
        }


def save_state(state: Dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "completed": list(state["completed"]),
            "failed": list(state["failed"]),
        }, f, ensure_ascii=False, indent=2)


def main(disk_path: str, local_root: str):
    try:
        test_resp = requests.get(
            BASE_URL,
            params={"path": disk_path},
            headers={"Authorization": f"OAuth {TOKEN}"},
            timeout=30,
        )
        test_resp.raise_for_status()
        if test_resp.json().get("type") != "dir":
            raise ValueError(f"{disk_path} — не папка или не найдена")
    except Exception as e:
        print(f"Ошибка доступа к корневой папке на Диске: {e}")
        return

    local_root = Path(local_root).resolve()
    local_root.mkdir(parents=True, exist_ok=True)

    state = load_state()
    print("🔄 Получение полного списка файлов с Яндекс Диска (это может занять время)...")
    try:
        all_files = list_folder(disk_path)
    except Exception as e:
        print(f"Ошибка при получении списка файлов: {e}")
        return

    print(f"📦 Всего найдено файлов для скачивания: {len(all_files)}")

    completed_count = 0
    failed_count = 0

    for i, f in enumerate(all_files, start=1):
        disk_path_file = f["path"]

        # Вычисляем локальный путь
        if disk_path == "/":
            rel_path = Path(disk_path_file).name
        else:
            try:
                rel_path = Path(disk_path_file).relative_to(disk_path)
            except ValueError:
                rel_path = Path(disk_path_file).name

        dest = local_root / rel_path

        # Проверка: если файл уже есть на диске и его размер совпадает — пропускаем
        if disk_path_file in state["completed"] or (dest.exists() and f.get("size") and dest.stat().st_size == f.get("size")):
            state["completed"].add(disk_path_file)
            print(f"[{i}/{len(all_files)}] Пропущено (уже скачано): {f['name']}")
            continue

        print(f"[{i}/{len(all_files)}] Скачивание: {f['name']}")
        dest.parent.mkdir(parents=True, exist_ok=True)

        ok = download_file(
            disk_path_file,
            dest,
            expected_size=f.get("size"),
            expected_md5=f.get("md5"),
        )

        if ok:
            state["completed"].add(disk_path_file)
            completed_count += 1
        else:
            state["failed"].add(disk_path_file)
            failed_count += 1

        save_state(state)
        time.sleep(0.3)  # небольшая пауза между файлами

    print(f"\n✨ Готово! Успешно скачано: {completed_count}, ошибок/не удалось: {failed_count}")
    print(f"📁 Состояние сохранено в {STATE_FILE}")


if __name__ == "__main__":
    DISK_PATH = "/Фотокамера"
    LOCAL_ROOT = r"D:\Жёсткий диск 1\Iphone 15 Pro 2024-2026"
    main(DISK_PATH, LOCAL_ROOT)