#!/usr/bin/env python3
"""
AstrBot Web 上传：zip 第一条记录必须是「一层文件夹」，见 AstrBot updator.unzip_file。

用法：
  python package_plugin.py          生成 astrbot_plugin_bktools.zip（直接上传）
  python package_plugin.py zip      同上
  python package_plugin.py folder   仅整理到 manual_upload/astrbot_plugin_bktools/，供你右键手动压缩
"""
from __future__ import annotations

import argparse
import os
import shutil
import zipfile

ROOT_NAME = "astrbot_plugin_bktools"
OUT_NAME = f"{ROOT_NAME}.zip"
MANUAL_REL = os.path.join("manual_upload", ROOT_NAME)

# 不打进包：工具脚本、已生成包、VCS 等
EXCLUDE_NAMES = frozenset(
    {
        OUT_NAME,
        "package_plugin.py",
        ".git",
        ".gitignore",
        ".cursor",
    }
)


def collect_plugin_filenames(base: str) -> list[str]:
    out: list[str] = []
    for name in os.listdir(base):
        if name in EXCLUDE_NAMES or name.startswith("."):
            continue
        path = os.path.join(base, name)
        if os.path.isfile(path):
            out.append(name)
    out.sort()
    if not out:
        raise SystemExit("No plugin files found next to package_plugin.py.")
    return out


def build_zip(base: str, files: list[str]) -> str:
    out_path = os.path.join(base, OUT_NAME)
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{ROOT_NAME}/", "")
        for name in files:
            zf.write(os.path.join(base, name), f"{ROOT_NAME}/{name}")
    return out_path


def build_manual_folder(base: str, files: list[str]) -> str:
    dest_root = os.path.join(base, MANUAL_REL)
    if os.path.isdir(dest_root):
        shutil.rmtree(dest_root)
    os.makedirs(dest_root, exist_ok=True)
    for name in files:
        shutil.copy2(os.path.join(base, name), os.path.join(dest_root, name))
    return dest_root


def main() -> None:
    parser = argparse.ArgumentParser(description="Pack astrbot_plugin_bktools for AstrBot Web upload.")
    parser.add_argument(
        "mode",
        nargs="?",
        default="zip",
        choices=("zip", "folder"),
        help="zip=生成可直接上传的zip；folder=只整理成一层文件夹供手动压缩 (default: zip)",
    )
    args = parser.parse_args()

    base = os.path.dirname(os.path.abspath(__file__))
    files = collect_plugin_filenames(base)

    if args.mode == "zip":
        out_path = build_zip(base, files)
        print(f"OK: {out_path}")
        print("在 AstrBot 插件安装里上传此 zip。")
        return

    dest = build_manual_folder(base, files)
    print(f"OK: {dest}")
    print("请在此路径的上一层 manual_upload 里：右键「astrbot_plugin_bktools」文件夹 → 发送到 → 压缩(zipped)文件夹。")
    print("不要进入文件夹全选内部文件压缩。")


if __name__ == "__main__":
    main()
