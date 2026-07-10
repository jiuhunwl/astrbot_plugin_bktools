#!/usr/bin/env python3
"""生成 AstrBot WebUI 可直接上传的插件压缩包。"""

from __future__ import annotations

import argparse
import os
import shutil
import zipfile


ROOT_NAME = "astrbot_plugin_bktools"
OUT_NAME = f"{ROOT_NAME}.zip"
MANUAL_REL = os.path.join("manual_upload", ROOT_NAME)
PLUGIN_FILES = (
    "CHANGELOG.md",
    "README.md",
    "_conf_schema.json",
    "logo.png",
    "main.py",
    "metadata.yaml",
    "requirements.txt",
)


def validate_files(base: str) -> None:
    missing = [name for name in PLUGIN_FILES if not os.path.isfile(os.path.join(base, name))]
    if missing:
        raise SystemExit("缺少插件文件: " + ", ".join(missing))


def build_zip(base: str) -> str:
    out_path = os.path.join(base, OUT_NAME)
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{ROOT_NAME}/", "")
        for name in PLUGIN_FILES:
            archive.write(os.path.join(base, name), f"{ROOT_NAME}/{name}")
    return out_path


def build_manual_folder(base: str) -> str:
    dest_root = os.path.join(base, MANUAL_REL)
    if os.path.isdir(dest_root):
        shutil.rmtree(dest_root)
    os.makedirs(dest_root, exist_ok=True)
    for name in PLUGIN_FILES:
        shutil.copy2(os.path.join(base, name), os.path.join(dest_root, name))
    return dest_root


def main() -> None:
    parser = argparse.ArgumentParser(description="打包 astrbot_plugin_bktools")
    parser.add_argument(
        "mode",
        nargs="?",
        default="zip",
        choices=("zip", "folder"),
        help="zip=生成上传包；folder=生成供手动压缩的目录",
    )
    args = parser.parse_args()

    base = os.path.dirname(os.path.abspath(__file__))
    validate_files(base)
    if args.mode == "folder":
        print(build_manual_folder(base))
    else:
        print(build_zip(base))


if __name__ == "__main__":
    main()
