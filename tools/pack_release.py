"""打发布包：只收运行必需的文件。

用法：
    python tools/pack_release.py            # 输出到 <workspace>/_pkg/
    python tools/pack_release.py <目录>      # 自定义输出目录

排除规则（这些是抓取缓存 / 中间备份，装给用户没用还占体积）：
    data/.xxx_cache.json   —— 抓站点的原始缓存
    data/*.liquipedia.json —— Liquipedia 中间产物（已合进正式题库）
    data/players.<日期>.json —— 旧快照备份
"""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = ROOT.name
SKIP = re.compile(r"(^\.|liquipedia\.json$|^players\.\d{4}-\d{2}-\d{2}\.json$)")


def collect() -> list[Path]:
    files = [
        p for p in ROOT.glob("*.py")
    ] + [
        ROOT / "metadata.yaml",
        ROOT / "_conf_schema.json",
        ROOT / "README.md",
        ROOT / "LICENSE",
        ROOT / "requirements.txt",
    ]
    files += sorted(ROOT.glob("assets/*"))
    files += sorted(ROOT.glob("data/*.json"))
    files += sorted(ROOT.glob("tests/*.py"))
    files += sorted(ROOT.glob("tools/*.py"))
    picked = []
    for path in files:
        if not path.is_file():
            continue
        if path.parent.name == "data" and SKIP.search(path.name):
            continue
        picked.append(path)
    return picked


def main() -> None:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT.parent / "_pkg"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{NAME}.zip"
    picked = collect()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in picked:
            zf.write(path, f"{NAME}/{path.relative_to(ROOT).as_posix()}")
    print(f"{target}  {target.stat().st_size / 1024:.1f} KB  {len(picked)} 个文件")
    for path in picked:
        print("  ", path.relative_to(ROOT).as_posix())


if __name__ == "__main__":
    main()
