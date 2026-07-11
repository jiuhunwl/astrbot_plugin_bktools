from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Iterable, Optional


REPOSITORY = "jiuhunwl/astrbot_plugin_bktools"
GITHUB_API = f"https://api.github.com/repos/{REPOSITORY}"
REQUIRED_FILES = {"main.py", "metadata.yaml", "_conf_schema.json"}
SKIP_NAMES = {".git", ".github", "__pycache__", "tests", "outputs", "manual_upload"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".zip"}


@dataclass(frozen=True)
class UpdateInfo:
    current_version: str
    latest_version: str
    download_url: str
    source: str
    release_url: str = ""
    notes: str = ""

    @property
    def available(self) -> bool:
        return compare_versions(self.latest_version, self.current_version) > 0


def normalize_version(value: str) -> str:
    text = str(value or "").strip()
    return text[1:] if text.lower().startswith("v") else text


def version_tuple(value: str) -> tuple[int, int, int, tuple[str, ...]]:
    text = normalize_version(value)
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:[-+]([0-9A-Za-z.-]+))?", text)
    if not match:
        raise ValueError(f"无效版本号: {value}")
    suffix_text = match.group(4) or ""
    suffix = tuple(suffix_text.split(".")) if suffix_text else ()
    return int(match.group(1)), int(match.group(2)), int(match.group(3)), suffix


def compare_versions(left: str, right: str) -> int:
    l = version_tuple(left)
    r = version_tuple(right)
    if l[:3] != r[:3]:
        return 1 if l[:3] > r[:3] else -1
    if not l[3] and r[3]:
        return 1
    if l[3] and not r[3]:
        return -1
    return (l[3] > r[3]) - (l[3] < r[3])


def metadata_version(text: str) -> str:
    match = re.search(r"(?m)^version:\s*['\"]?([^'\"\s]+)", text)
    if not match:
        raise ValueError("metadata.yaml 缺少 version")
    version_tuple(match.group(1))
    return match.group(1)


def metadata_name(text: str) -> str:
    match = re.search(r"(?m)^name:\s*['\"]?([^'\"\s]+)", text)
    if not match:
        raise ValueError("metadata.yaml 缺少 name")
    return match.group(1)


def safe_archive_members(archive: zipfile.ZipFile) -> Iterable[zipfile.ZipInfo]:
    for info in archive.infolist():
        path = Path(info.filename.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"更新包包含不安全路径: {info.filename}")
        if info.is_dir():
            continue
        yield info


def _find_source_root(root: Path) -> Path:
    candidates = [path.parent for path in root.rglob("metadata.yaml")]
    for candidate in candidates:
        if all((candidate / name).is_file() for name in REQUIRED_FILES):
            return candidate
    raise ValueError("更新包缺少插件必要文件")


def _iter_update_files(source: Path) -> Iterable[Path]:
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        if any(part in SKIP_NAMES or part.startswith(".bktools_backup") for part in relative.parts):
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        yield path


def install_archive(
    archive_bytes: bytes,
    plugin_dir: str,
    *,
    expected_version: str,
    expected_name: str = "astrbot_plugin_bktools",
) -> tuple[str, int]:
    """Validate, back up and install a trusted GitHub source archive."""
    plugin_root = Path(plugin_dir).resolve()
    staging = Path(tempfile.mkdtemp(prefix="bktools_update_"))
    backup_root = plugin_root / ".bktools_backup"
    backup = backup_root / time.strftime("%Y%m%d-%H%M%S")
    changed: list[tuple[Path, Optional[Path]]] = []
    try:
        with zipfile.ZipFile(BytesIO(archive_bytes)) as archive:
            members = list(safe_archive_members(archive))
            for info in members:
                target = (staging / info.filename).resolve()
                if staging != target and staging not in target.parents:
                    raise ValueError(f"更新包路径越界: {info.filename}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)

        source_root = _find_source_root(staging)
        metadata = (source_root / "metadata.yaml").read_text(encoding="utf-8-sig")
        if metadata_name(metadata) != expected_name:
            raise ValueError("更新包插件名称不匹配")
        archive_version = metadata_version(metadata)
        if compare_versions(archive_version, expected_version) != 0:
            raise ValueError(
                f"更新包版本不匹配: 期望 {expected_version}，实际 {archive_version}"
            )

        files = list(_iter_update_files(source_root))
        if not files:
            raise ValueError("更新包没有可安装文件")
        backup.mkdir(parents=True, exist_ok=False)
        for source in files:
            relative = source.relative_to(source_root)
            destination = (plugin_root / relative).resolve()
            if plugin_root != destination and plugin_root not in destination.parents:
                raise ValueError(f"安装目标路径越界: {relative}")
            previous: Optional[Path] = None
            if destination.exists():
                previous = backup / relative
                previous.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(destination, previous)
            destination.parent.mkdir(parents=True, exist_ok=True)
            temp_destination = destination.with_name(destination.name + ".bktools-new")
            shutil.copy2(source, temp_destination)
            os.replace(temp_destination, destination)
            changed.append((destination, previous))
        return str(backup), len(files)
    except Exception:
        for destination, previous in reversed(changed):
            try:
                if previous and previous.exists():
                    shutil.copy2(previous, destination)
                elif destination.exists():
                    destination.unlink()
            except OSError:
                pass
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
