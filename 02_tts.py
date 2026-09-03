#!/usr/bin/env python3
"""02 TTS: output/{id}/script.json → 문장별 음성 → output/{id}/narration.mp3 + audio.json

Naver Clova Voice (Premium) 를 쓴다. 문장마다 따로 합성해 길이를 재고 이어 붙이므로
03 자막 타이밍은 audio.json 의 문장별 시작/끝 시각을 그대로 쓰면 된다.

사용법:
    python 02_tts.py                    # topics.csv 첫 행 (output/001/script.json)
    python 02_tts.py --id 001
    python 02_tts.py --dry-run          # API 호출 없이 요청 내용만 출력 (무료)
    python 02_tts.py --script 경로.json --dry-run   # 다른 대본 파일로 시험
    python 02_tts.py --speaker nminsang --speed 2

비용: 문장 수만큼 Clova Voice 호출 (글자 수 기준 과금). 실행 전 확인할 것.
키: NCP_CLOVA_CLIENT_ID / NCP_CLOVA_CLIENT_SECRET 환경변수(또는 .env)에서만 읽는다.
필요 도구: ffmpeg, ffprobe (이어 붙이기·길이 측정). --dry-run 은 없어도 된다.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from pipeline.common import OUTPUT_DIR, load_dotenv, pick_topic

CLOVA_URL = "https://naveropenapi.apigw.ntruss.com/tts-premium/v1/tts"
ENV_ID = "NCP_CLOVA_CLIENT_ID"
ENV_SECRET = "NCP_CLOVA_CLIENT_SECRET"

# 어르신 대상: 또렷하고 차분한 목소리. 환경변수 CLOVA_SPEAKER 로 바꿀 수 있다.
DEFAULT_SPEAKER = os.environ.get("CLOVA_SPEAKER", "nara")
# Clova speed: -5(빠름) ~ 5(느림), 0 기본. CLAUDE.md 의 0.85x 에 맞춰 1을 기본으로 두고
# 첫 결과를 들어본 뒤 조정한다. (Clova 는 배속을 숫자로 받지 않는다)
DEFAULT_SPEED = 1
PAUSE_SEC = 0.5          # 문장 사이 쉬는 시간
MAX_TEXT_CHARS = 2000    # Clova 요청당 글자 제한


def load_script(topic_id: str | None, row: int | None, script_arg: str | None) -> tuple[dict, Path]:
    if script_arg:
        path = Path(script_arg)
    else:
        topic = pick_topic(topic_id, row)
        path = topic.script_path
    if not path.exists():
        raise FileNotFoundError(f"대본이 없습니다: {path}  (먼저 01_write.py 를 실행)")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not data.get("sentences"):
        raise ValueError(f"{path} 에 sentences 가 없습니다.")
    return data, path


def plan_requests(script: dict, speaker: str, speed: int) -> list[dict]:
    """문장 하나 = 요청 하나. 실제 전송 본문(form) 을 미리 만든다."""
    reqs = []
    for i, sent in enumerate(script["sentences"], 1):
        text = sent.strip()
        if len(text) > MAX_TEXT_CHARS:
            raise ValueError(f"{i}번 문장이 {MAX_TEXT_CHARS}자를 넘습니다.")
        reqs.append({
            "index": i,
            "file": f"{i:02d}.mp3",
            "form": {"speaker": speaker, "speed": speed, "volume": 0, "pitch": 0,
                     "format": "mp3", "text": text},
        })
    return reqs


def synthesize(form: dict, out: Path) -> None:
    import requests

    cid, secret = os.environ.get(ENV_ID), os.environ.get(ENV_SECRET)
    if not cid or not secret:
        sys.exit(f"{ENV_ID} / {ENV_SECRET} 환경변수가 없습니다. 파일에 키를 쓰지 말고 환경변수로 넣으세요.")
    r = requests.post(
        CLOVA_URL,
        headers={"X-NCP-APIGW-API-KEY-ID": cid, "X-NCP-APIGW-API-KEY": secret,
                 "Content-Type": "application/x-www-form-urlencoded"},
        data=form, timeout=60,
    )
    if r.status_code != 200:
        sys.exit(f"Clova 오류 {r.status_code}: {r.text[:300]}")
    out.write_bytes(r.content)


def duration_sec(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return float(out)


def concat_with_pauses(parts: list[Path], out: Path, pause: float) -> None:
    """문장 mp3 들을 pause 초 무음으로 이어 붙인다."""
    args = ["ffmpeg", "-y", "-v", "error"]
    for p in parts:
        args += ["-i", str(p)]
    n = len(parts)
    chain = "".join(f"[{i}:a]" + (f"apad=pad_dur={pause}," if i < n - 1 else "") + f"aformat=sample_rates=24000:channel_layouts=mono[a{i}];"
                    for i in range(n))
    chain += "".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[out]"
    args += ["-filter_complex", chain, "-map", "[out]", "-c:a", "libmp3lame", "-q:a", "2", str(out)]
    subprocess.run(args, check=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--id", help="topics.csv 의 id")
    g.add_argument("--row", type=int, help="topics.csv 행 번호 (1부터)")
    ap.add_argument("--script", help="script.json 경로를 직접 지정")
    ap.add_argument("--speaker", default=DEFAULT_SPEAKER, help=f"Clova 화자 (기본 {DEFAULT_SPEAKER})")
    ap.add_argument("--speed", type=int, default=DEFAULT_SPEED, help="-5(빠름)~5(느림), 기본 1")
    ap.add_argument("--dry-run", action="store_true", help="API 호출 없이 요청 내용만 출력")
    ap.add_argument("--force", action="store_true", help="기존 narration.mp3 를 덮어쓴다")
    args = ap.parse_args()

    if not -5 <= args.speed <= 5:
        ap.error("--speed 는 -5~5 사이여야 합니다.")

    load_dotenv()
    try:
        script, script_path = load_script(args.id, args.row, args.script)
    except (FileNotFoundError, ValueError) as e:
        sys.exit(str(e))
    work_dir = script_path.parent
    reqs = plan_requests(script, args.speaker, args.speed)
    total_chars = sum(len(r["form"]["text"]) for r in reqs)

    if args.dry_run:
        print(f"=== dry-run: {script_path} ===")
        print(f"endpoint : {CLOVA_URL}")
        print(f"headers  : X-NCP-APIGW-API-KEY-ID, X-NCP-APIGW-API-KEY  (환경변수 {ENV_ID}/{ENV_SECRET})")
        print(f"speaker  : {args.speaker}   speed: {args.speed}   pause: {PAUSE_SEC}s   format: mp3")
        for r in reqs:
            print(f"  {r['file']}  ({len(r['form']['text']):2d}자)  {r['form']['text']}")
        print(f"\n요청 {len(reqs)}회, 총 {total_chars}자 (Clova 과금 기준). 출력 예정: {work_dir / 'narration.mp3'}, {work_dir / 'audio.json'}")
        have_ffmpeg = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
        print(f"ffmpeg/ffprobe: {'있음' if have_ffmpeg else '없음 — 실제 실행 전에 설치 필요'}")
        print("[dry-run] API 는 호출하지 않았습니다.")
        return 0

    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        sys.exit("ffmpeg/ffprobe 가 필요합니다.")
    final = work_dir / "narration.mp3"
    if final.exists() and not args.force:
        print(f"이미 있음: {final} (--force 로 재생성)")
        return 0

    tts_dir = work_dir / "tts"
    tts_dir.mkdir(parents=True, exist_ok=True)
    print(f"[{script.get('id', '?')}] Clova Voice {args.speaker} speed={args.speed}, {len(reqs)}문장 {total_chars}자")

    segments, t = [], 0.0
    for r in reqs:
        out = tts_dir / r["file"]
        synthesize(r["form"], out)
        d = duration_sec(out)
        segments.append({"index": r["index"], "text": r["form"]["text"], "file": str(out.relative_to(work_dir)),
                         "start": round(t, 3), "end": round(t + d, 3), "duration": round(d, 3)})
        print(f"  {r['file']}  {d:5.2f}s  {r['form']['text']}")
        t += d + PAUSE_SEC
    total = t - PAUSE_SEC

    concat_with_pauses([tts_dir / r["file"] for r in reqs], final, PAUSE_SEC)
    audio = {
        "id": script.get("id"), "narration": final.name, "speaker": args.speaker, "speed": args.speed,
        "pause_sec": PAUSE_SEC, "total_sec": round(total, 3), "segments": segments,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (work_dir / "audio.json").write_text(json.dumps(audio, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n총 {total:.1f}초 → {final.relative_to(OUTPUT_DIR.parent)}, audio.json")
    if not 40 <= total <= 50:
        print(f"⚠ 목표 40~50초를 벗어났습니다 ({total:.1f}초). --speed 나 대본 길이를 조정하세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
