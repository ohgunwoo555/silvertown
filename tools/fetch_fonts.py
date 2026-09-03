#!/usr/bin/env python3
"""자막 폰트 내려받기 → assets/fonts/

1순위 Pretendard Bold (GitHub 릴리스 zip, SIL OFL 1.1)
2순위 Pretendard Bold (저장소 raw 파일)
3순위 NanumGothic Bold (google/fonts 저장소, SIL OFL 1.1)
폰트 파일과 라이선스 파일을 함께 저장하고, 성공한 폰트의 family 이름을 assets/fonts/FONT.json 에 기록한다.
03_subtitle.py 와 05_render.py 는 FONT.json 을 읽어 .ass 폰트명과 fontsdir 를 정한다.

사용법: python tools/fetch_fonts.py [--force]
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FONT_DIR = ROOT / "assets" / "fonts"

PRETENDARD_VER = "1.3.9"
PRETENDARD_ZIP = f"https://github.com/orioncactus/pretendard/releases/download/v{PRETENDARD_VER}/Pretendard-{PRETENDARD_VER}.zip"
PRETENDARD_RAW = "https://raw.githubusercontent.com/orioncactus/pretendard/main/packages/pretendard/dist/public/static/Pretendard-Bold.otf"
PRETENDARD_LICENSE = "https://raw.githubusercontent.com/orioncactus/pretendard/main/LICENSE"
NANUM_RAW = "https://raw.githubusercontent.com/google/fonts/main/ofl/nanumgothic/NanumGothic-Bold.ttf"
NANUM_LICENSE = "https://raw.githubusercontent.com/google/fonts/main/ofl/nanumgothic/OFL.txt"

CANDIDATES = [
    {"name": "Pretendard 릴리스 zip", "family": "Pretendard", "file": "Pretendard-Bold.otf",
     "license_file": "LICENSE-Pretendard.txt", "kind": "zip", "url": PRETENDARD_ZIP,
     "zip_member": "Pretendard-Bold.otf", "zip_license": "LICENSE", "license_url": PRETENDARD_LICENSE},
    {"name": "Pretendard raw", "family": "Pretendard", "file": "Pretendard-Bold.otf",
     "license_file": "LICENSE-Pretendard.txt", "kind": "raw", "url": PRETENDARD_RAW, "license_url": PRETENDARD_LICENSE},
    {"name": "NanumGothic (google/fonts)", "family": "NanumGothic", "file": "NanumGothic-Bold.ttf",
     "license_file": "LICENSE-NanumGothic.txt", "kind": "raw", "url": NANUM_RAW, "license_url": NANUM_LICENSE},
]


def fetch(url: str, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "silvertown-fetch-fonts"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def try_candidate(c: dict) -> tuple[bytes, bytes]:
    """(폰트 바이트, 라이선스 바이트)"""
    if c["kind"] == "zip":
        blob = fetch(c["url"], timeout=300)
        zf = zipfile.ZipFile(io.BytesIO(blob))
        names = zf.namelist()
        font_name = next((n for n in names if n.endswith("/" + c["zip_member"]) or n == c["zip_member"]), None)
        if not font_name:
            raise FileNotFoundError(f"zip 안에 {c['zip_member']} 이 없습니다")
        lic_name = next((n for n in names if n.split("/")[-1] == c["zip_license"]), None)
        lic = zf.read(lic_name) if lic_name else fetch(c["license_url"])
        return zf.read(font_name), lic
    return fetch(c["url"]), fetch(c["license_url"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true", help="이미 있어도 다시 받는다")
    args = ap.parse_args()
    FONT_DIR.mkdir(parents=True, exist_ok=True)
    meta_path = FONT_DIR / "FONT.json"

    if meta_path.exists() and not args.force:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if (FONT_DIR / meta["file"]).exists():
            print(f"이미 있음: {meta['family']} → {FONT_DIR / meta['file']} (--force 로 다시 받기)")
            return 0

    for c in CANDIDATES:
        print(f"시도: {c['name']} … ", end="", flush=True)
        try:
            font, lic = try_candidate(c)
        except Exception as e:  # 네트워크·정책 오류는 다음 후보로
            print(f"실패 ({type(e).__name__}: {str(e)[:80]})")
            continue
        if len(font) < 100_000:
            print(f"실패 (파일이 너무 작음: {len(font)} bytes)")
            continue
        (FONT_DIR / c["file"]).write_bytes(font)
        (FONT_DIR / c["license_file"]).write_bytes(lic)
        meta = {"family": c["family"], "file": c["file"], "license": c["license_file"],
                "source": c["url"], "bytes": len(font)}
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"완료 ({len(font) // 1024} KB)")
        print(f"폰트: {FONT_DIR / c['file']}\n라이선스: {FONT_DIR / c['license_file']}\nfamily: {c['family']} → FONT.json")
        return 0

    print("모든 후보에서 폰트를 받지 못했습니다. 네트워크 정책을 확인하거나 assets/fonts/ 에 직접 넣으세요.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
