#!/usr/bin/env python3
"""从发布清单中移除仅供本机抓取定位使用的临时目录字段。"""
import argparse
import json
from pathlib import Path


def clean(value):
    if isinstance(value, dict):
        return {key: clean(item) for key, item in value.items() if key not in {"download_root", "download_directory"}}
    if isinstance(value, list):
        return [clean(item) for item in value]
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    data = json.loads(args.manifest.read_text(encoding="utf-8"))
    result = clean(data)
    args.manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已清理本机临时字段，保留 {len(result['pages'])} 页及全部其他清单内容")


if __name__ == "__main__":
    main()
