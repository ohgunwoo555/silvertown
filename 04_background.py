#!/usr/bin/env python3
"""04 배경 소재: subtitles.json 의 문장 타이밍 → 컷 계획 + 소재 배정 → output/{id}/background.json

우선순위
  1. 로컬  assets/backgrounds/{id}/  →  assets/backgrounds/   (영상·이미지)
  2. 이미지 생성 (IMAGE_PROVIDER=fal, FAL_KEY 필요): 컷당 한 장 → output/{id}/bg_{n}.png
     프롬프트 = script.json 의 문장별 image_prompt + templates/image_style.md 의 style/avoid
  3. Pexels 영상 검색 (USE_PEXELS=true 일 때만, PEXELS_API_KEY 필요)
  4. 단색 그라데이션 PNG 자동 생성 (아무것도 없거나 키가 없을 때. 렌더가 멈추지 않게 한다)

컷은 문장 경계에서만 바뀌고 한 컷은 3초 이상이다 (CLAUDE.md 영상 규칙).

사용법:
    python 04_background.py                # topics.csv 첫 행
    python 04_background.py --id 001
    python 04_background.py --dry-run      # 컷 계획·소재·이미지 프롬프트만 출력, 파일 안 만듦
    python 04_background.py --gradient     # 로컬·생성·Pexels 를 무시하고 그라데이션만 사용
    IMAGE_PROVIDER=fal python 04_background.py --dry-run   # 생성 프롬프트 확인

비용: IMAGE_PROVIDER=fal 이면 컷 수만큼 fal.ai 이미지 생성 과금, USE_PEXELS=true 면 Pexels 호출. 실행 전 확인할 것.
키: FAL_KEY, PEXELS_API_KEY 환경변수(또는 .env)에서만 읽는다.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import struct
import sys
import zlib
from datetime import datetime, timezone
from pathlib import Path

from pipeline.common import ASSETS_DIR, OUTPUT_DIR, load_dotenv, load_image_style, pick_topic

BG_DIR = ASSETS_DIR / "backgrounds"
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png"}
MIN_CUT_SEC = 3.0
WIDTH, HEIGHT = 1080, 1920

PEXELS_URL = "https://api.pexels.com/videos/search"
PEXELS_PER_PAGE = 5

# --- 이미지 생성 (fal.ai) ---
IMAGE_PROVIDERS = ("none", "fal")
FAL_URL = "https://fal.run/{model}"                 # 동기 엔드포인트
DEFAULT_FAL_MODEL = "fal-ai/flux/schnell"            # 환경변수 FAL_MODEL 로 변경
DEFAULT_FAL_SIZE = "portrait_16_9"                   # 또는 "WxH" (예: 1088x1920). 05 가 1080x1920 으로 채운다

# 어르신 눈에 편한 차분한 색. (위 색, 아래 색) 쌍으로 세로 그라데이션.
GRADIENTS = [
    ((0x2E, 0x5C, 0x8A), (0x8F, 0xC1, 0xE3)),   # 하늘·바다
    ((0x3C, 0x6E, 0x47), (0xA8, 0xD5, 0xA2)),   # 숲
    ((0xC7, 0x6B, 0x3A), (0xF4, 0xD2, 0xA0)),   # 노을
    ((0x4A, 0x3F, 0x6B), (0xB8, 0xA9, 0xD9)),   # 라벤더
    ((0x2B, 0x6F, 0x7A), (0x9F, 0xD8, 0xD0)),   # 청록
    ((0x7A, 0x4E, 0x2D), (0xE0, 0xC3, 0x9A)),   # 흙·모래
]


# ---------- 컷 계획 ----------

def sentence_spans(cues: list[dict]) -> list[dict]:
    """자막 큐를 문장 단위로 합쳐 (index, start, end) 목록으로."""
    spans: dict[int, dict] = {}
    for c in cues:
        s = spans.setdefault(c["sentence"], {"index": c["sentence"], "start": c["start"], "end": c["end"]})
        s["start"], s["end"] = min(s["start"], c["start"]), max(s["end"], c["end"])
    return [spans[k] for k in sorted(spans)]


def plan_cuts(spans: list[dict], total: float, min_cut: float = MIN_CUT_SEC) -> list[dict]:
    """문장 경계에서만 컷을 바꾸되 한 컷이 min_cut 초 이상 되도록 문장을 묶는다."""
    cuts, cur = [], None
    for sp in spans:
        if cur is None:
            cur = {"start": sp["start"], "sentences": [sp["index"]]}
            continue
        if sp["start"] - cur["start"] >= min_cut:          # 지금까지 묶은 게 충분히 길면 새 컷
            cur["end"] = sp["start"]
            cuts.append(cur)
            cur = {"start": sp["start"], "sentences": [sp["index"]]}
        else:
            cur["sentences"].append(sp["index"])
    if cur:
        cur["end"] = total
        if cuts and cur["end"] - cur["start"] < min_cut:    # 마지막이 짧으면 앞 컷에 합침
            cuts[-1]["end"] = total
            cuts[-1]["sentences"] += cur["sentences"]
        else:
            cuts.append(cur)
    for i, c in enumerate(cuts, 1):
        c["index"] = i
        c["duration"] = round(c["end"] - c["start"], 3)
        c["start"], c["end"] = round(c["start"], 3), round(c["end"], 3)
    return cuts


# ---------- 소재 ----------

def local_assets(topic_id: str) -> tuple[list[Path], str]:
    for folder, label in ((BG_DIR / topic_id, f"assets/backgrounds/{topic_id}/"), (BG_DIR, "assets/backgrounds/")):
        if folder.is_dir():
            files = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTS | IMAGE_EXTS)
            if files:
                return files, label
    return [], ""


def assign_round_robin(files: list[Path], n: int, seed: str) -> list[Path]:
    """같은 id 면 항상 같은 순서. 연속 컷에 같은 파일이 오지 않게 한다 (파일이 2개 이상일 때)."""
    order = files[:]
    random.Random(seed).shuffle(order)
    out = []
    for i in range(n):
        cand = order[i % len(order)]
        if out and cand == out[-1] and len(order) > 1:
            cand = order[(i + 1) % len(order)]
        out.append(cand)
    return out


def pexels_search(query: str, api_key: str) -> list[dict]:
    import requests
    r = requests.get(PEXELS_URL, headers={"Authorization": api_key},
                     params={"query": query, "orientation": "portrait", "size": "medium", "per_page": PEXELS_PER_PAGE},
                     timeout=30)
    if r.status_code != 200:
        sys.exit(f"Pexels 오류 {r.status_code}: {r.text[:200]}")
    return r.json().get("videos", [])


def pexels_pick_file(video: dict) -> dict | None:
    """세로 영상 파일 중 높이가 1920 에 가장 가까운 것 (없으면 가장 큰 것)."""
    files = [f for f in video.get("video_files", []) if f.get("file_type") == "video/mp4" and f.get("height")]
    portrait = [f for f in files if f["height"] > f.get("width", 0)]
    pool = portrait or files
    if not pool:
        return None
    return min(pool, key=lambda f: abs(f["height"] - HEIGHT))


def fetch_pexels(keywords: list[str], n: int, api_key: str, bg_dir: Path) -> list[dict]:
    import requests
    picked: list[dict] = []
    seen: set[int] = set()
    for i in range(n):
        kw = keywords[i % len(keywords)]
        for v in pexels_search(kw, api_key):
            if v["id"] in seen:
                continue
            f = pexels_pick_file(v)
            if not f:
                continue
            seen.add(v["id"])
            dest = bg_dir / f"pexels_{v['id']}.mp4"
            if not dest.exists():
                with requests.get(f["link"], stream=True, timeout=120) as r:
                    r.raise_for_status()
                    with dest.open("wb") as fh:
                        for chunk in r.iter_content(1 << 16):
                            fh.write(chunk)
            picked.append({"file": str(dest), "keyword": kw, "pexels_id": v["id"], "url": v.get("url"),
                           "photographer": v.get("user", {}).get("name"), "duration": v.get("duration")})
            break
        if len(picked) <= i:
            print(f"⚠ Pexels 에서 '{kw}' 결과가 없습니다.")
    return picked


# ---------- 이미지 생성 (fal.ai) ----------

def build_image_prompt(cut: dict, script: dict, style: dict) -> str:
    """컷의 첫 문장 image_prompt + 고정 스타일 + 피할 것. image_prompt 가 없으면 keywords 로 대신."""
    prompts = script.get("image_prompts") or []
    first = cut["sentences"][0] - 1
    scene = prompts[first].strip() if first < len(prompts) and prompts[first].strip() else ""
    if not scene:
        scene = "a calm everyday scene about " + ", ".join(script.get("keywords") or [script.get("topic", "health")])
    scene = scene.rstrip(".")
    return f"{scene}. {style['style']}. Avoid: {style['avoid']}."


def parse_fal_size(value: str):
    m = re.fullmatch(r"(\d{3,4})x(\d{3,4})", value.strip())
    return {"width": int(m.group(1)), "height": int(m.group(2))} if m else value.strip()


def fal_generate(prompt: str, out: Path, seed: int) -> dict:
    """fal.ai 동기 호출 → 이미지 한 장 저장. 응답의 url/seed 를 돌려준다."""
    import requests
    key = os.environ["FAL_KEY"]
    model = os.environ.get("FAL_MODEL", DEFAULT_FAL_MODEL)
    body = {"prompt": prompt, "image_size": parse_fal_size(os.environ.get("FAL_IMAGE_SIZE", DEFAULT_FAL_SIZE)),
            "num_images": 1, "output_format": "png", "enable_safety_checker": True, "seed": seed}
    r = requests.post(FAL_URL.format(model=model), headers={"Authorization": f"Key {key}"}, json=body, timeout=180)
    if r.status_code != 200:
        sys.exit(f"fal.ai 오류 {r.status_code}: {r.text[:300]}")
    data = r.json()
    images = data.get("images") or []
    if not images or not images[0].get("url"):
        sys.exit(f"fal.ai 응답에 이미지가 없습니다: {str(data)[:300]}")
    img = requests.get(images[0]["url"], timeout=120)
    img.raise_for_status()
    out.write_bytes(img.content)
    return {"url": images[0]["url"], "seed": data.get("seed", seed), "model": model,
            "width": images[0].get("width"), "height": images[0].get("height")}


# ---------- 그라데이션 PNG (외부 라이브러리 없이) ----------

def write_gradient_png(path: Path, top: tuple, bottom: tuple, w: int = WIDTH, h: int = HEIGHT) -> None:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    rows = bytearray()
    for y in range(h):
        t = y / (h - 1)
        r, g, b = (round(top[k] + (bottom[k] - top[k]) * t) for k in range(3))
        rows += b"\x00" + bytes((r, g, b)) * w
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) \
        + chunk(b"IDAT", zlib.compress(bytes(rows), 6)) + chunk(b"IEND", b"")
    path.write_bytes(png)


# ---------- main ----------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--id", help="topics.csv 의 id")
    g.add_argument("--row", type=int, help="topics.csv 행 번호 (1부터)")
    ap.add_argument("--dry-run", action="store_true", help="계획·프롬프트만 출력, 파일 안 만듦")
    ap.add_argument("--gradient", action="store_true", help="로컬·생성·Pexels 무시, 그라데이션만")
    ap.add_argument("--image-provider", choices=IMAGE_PROVIDERS, default=None,
                    help="이미지 생성 백엔드 (기본: 환경변수 IMAGE_PROVIDER, 없으면 none)")
    ap.add_argument("--force", action="store_true", help="이미 있는 bg_{n}.png 도 다시 생성")
    args = ap.parse_args()

    load_dotenv()
    try:
        topic = pick_topic(args.id, args.row)
    except (FileNotFoundError, ValueError) as e:
        sys.exit(str(e))
    work_dir = topic.work_dir
    subs_path, script_path = work_dir / "subtitles.json", topic.script_path
    if not subs_path.exists():
        sys.exit(f"subtitles.json 이 없습니다: {subs_path}  (먼저 03_subtitle.py)")
    subs = json.loads(subs_path.read_text(encoding="utf-8"))
    script = json.loads(script_path.read_text(encoding="utf-8")) if script_path.exists() else {}
    keywords = script.get("keywords", [])

    cuts = plan_cuts(sentence_spans(subs["cues"]), subs["total_sec"])
    use_pexels = os.environ.get("USE_PEXELS", "false").strip().lower() in ("1", "true", "yes", "on")
    image_provider = (args.image_provider or os.environ.get("IMAGE_PROVIDER", "none")).strip().lower()
    if image_provider not in IMAGE_PROVIDERS:
        sys.exit(f"IMAGE_PROVIDER={image_provider!r} 는 지원하지 않습니다. 가능: {', '.join(IMAGE_PROVIDERS)}")

    # 소재 출처 결정: 로컬 → 생성(fal) → Pexels → 그라데이션
    source, files, label = "gradient", [], ""
    if not args.gradient:
        files, label = local_assets(topic.id)
        if files:
            source = "local"
        elif image_provider == "fal":
            source = "fal"
        elif use_pexels:
            source = "pexels"
    if source == "fal" and not os.environ.get("FAL_KEY"):
        print("⚠ IMAGE_PROVIDER=fal 이지만 FAL_KEY 가 없어 " + ("Pexels 로" if use_pexels and os.environ.get("PEXELS_API_KEY") else "그라데이션으로") + " 대체합니다.")
        source = "pexels" if use_pexels and os.environ.get("PEXELS_API_KEY") else "gradient"
    if source == "pexels" and not os.environ.get("PEXELS_API_KEY"):
        print("⚠ USE_PEXELS=true 이지만 PEXELS_API_KEY 가 없어 그라데이션으로 대체합니다.")
        source = "gradient"

    style = load_image_style() if (image_provider == "fal" or args.dry_run) else None
    print(f"[{topic.id}] 총 {subs['total_sec']:.1f}초 → 컷 {len(cuts)}개 (최소 {MIN_CUT_SEC}초) / 소재: {source}"
          + (f" ({label}, {len(files)}개)" if source == "local" else "")
          + (f" ({os.environ.get('FAL_MODEL', DEFAULT_FAL_MODEL)}, {os.environ.get('FAL_IMAGE_SIZE', DEFAULT_FAL_SIZE)})" if source == "fal" else "")
          + (f" (USE_PEXELS={use_pexels}, keywords={keywords})" if source == "pexels" else ""))

    if args.dry_run:
        show_prompts = image_provider == "fal" or source == "fal"
        if show_prompts:
            print(f"  공통 style : {style['style']}\n  공통 avoid : {style['avoid']}\n  (컷별 프롬프트 = 아래 장면 + 공통 style + 'Avoid: ' + 공통 avoid)")
        for c in cuts:
            print(f"  컷{c['index']:2d} {c['start']:6.2f} → {c['end']:6.2f} ({c['duration']:.1f}s) 문장 {c['sentences']}")
            if show_prompts:
                scene = build_image_prompt(c, script, style).split(". " + style["style"])[0]
                print(f"        bg_{c['index']:02d}.png ← {scene}")
        if image_provider == "fal":
            print(f"FAL_KEY: {'있음' if os.environ.get('FAL_KEY') else '없음 (실행 시 그라데이션 폴백)'}  "
                  f"— 실제 실행 시 fal.ai 이미지 생성 {len(cuts)}장 과금")
        print("[dry-run] 파일을 만들지 않았습니다.")
        return 0

    bg_dir = work_dir / "bg"
    bg_dir.mkdir(parents=True, exist_ok=True)
    attribution: list[dict] = []

    if source == "local":
        chosen = assign_round_robin(files, len(cuts), topic.id)
        for c, f in zip(cuts, chosen):
            c.update({"type": "video" if f.suffix.lower() in VIDEO_EXTS else "image", "file": str(f)})
    elif source == "fal":
        print(f"  fal.ai 이미지 {len(cuts)}장 생성 …")
        for c in cuts:
            out = work_dir / f"bg_{c['index']:02d}.png"
            prompt = build_image_prompt(c, script, style)
            seed = int(topic.id) * 1000 + c["index"] if topic.id.isdigit() else c["index"]
            if out.exists() and not args.force:
                meta = {"reused": True}
            else:
                meta = fal_generate(prompt, out, seed)
                if not out.exists() or out.stat().st_size < 10_000:
                    sys.exit(f"생성 이미지가 비정상입니다: {out}")
            c.update({"type": "image", "file": str(out), "prompt": prompt, "fal": meta})
            print(f"    bg_{c['index']:02d}.png {'(기존)' if meta.get('reused') else '생성'}  {prompt[:70]}…")
        attribution = [{"provider": "fal.ai", "model": os.environ.get("FAL_MODEL", DEFAULT_FAL_MODEL)}]
    elif source == "pexels":
        picked = fetch_pexels(keywords or [topic.topic], len(cuts), os.environ["PEXELS_API_KEY"], bg_dir)
        if not picked:
            print("⚠ Pexels 결과가 없어 그라데이션으로 대체합니다.")
            source = "gradient"
        else:
            attribution = picked
            for i, c in enumerate(cuts):
                p = picked[i % len(picked)]
                c.update({"type": "video", "file": p["file"], "pexels_id": p["pexels_id"]})
    if source == "gradient":
        for i, c in enumerate(cuts):
            top, bottom = GRADIENTS[i % len(GRADIENTS)]
            path = bg_dir / f"gradient_{c['index']:02d}.png"
            write_gradient_png(path, top, bottom)
            c.update({"type": "image", "file": str(path), "colors": ["#%02X%02X%02X" % top, "#%02X%02X%02X" % bottom]})

    result = {
        "id": topic.id, "source": source, "total_sec": subs["total_sec"], "min_cut_sec": MIN_CUT_SEC,
        "width": WIDTH, "height": HEIGHT, "cuts": cuts, "attribution": attribution,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (work_dir / "background.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    for c in cuts:
        print(f"  컷{c['index']:2d} {c['start']:6.2f} → {c['end']:6.2f} ({c['duration']:.1f}s) 문장 {c['sentences']}  {c['type']}: {Path(c['file']).name}")
    print(f"→ {(work_dir / 'background.json').relative_to(OUTPUT_DIR.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
