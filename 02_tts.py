#!/usr/bin/env python3
"""02 TTS: output/{id}/script.json → 문장별 음성 → output/{id}/narration.mp3 + audio.json

백엔드 (TTS_PROVIDER 환경변수 또는 --provider):
  clova  Naver Clova Voice Premium. 키 필요 (NCP_CLOVA_CLIENT_ID / NCP_CLOVA_CLIENT_SECRET), 글자 수 과금. 기본값.
  edge   Microsoft Edge 음성 (edge-tts 패키지). 키 없음, 무료. 인터넷 연결만 있으면 된다.

문장마다 따로 합성해 길이를 재고 이어 붙이므로 03 자막 타이밍은 audio.json 의 문장별 시작/끝 시각을 그대로 쓴다.

사용법:
    python 02_tts.py                    # topics.csv 첫 행 (output/001/script.json)
    python 02_tts.py --id 001
    python 02_tts.py --dry-run          # 호출 없이 요청 내용만 출력
    python 02_tts.py --script 경로.json --dry-run
    TTS_PROVIDER=edge python 02_tts.py               # 키 없이
    python 02_tts.py --provider edge --voice ko-KR-InJoonNeural --rate -15%
    python 02_tts.py --provider clova --speaker nminsang --speed 2

속도: CLAUDE.md 의 0.85배속. edge 는 --rate -15% 로 정확히 맞추고, clova 는 정수 단계(--speed, 기본 1)라 들어보고 조정한다.
필요 도구: ffmpeg, ffprobe (이어 붙이기·길이 측정). --dry-run 은 없어도 된다.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from pipeline.common import OUTPUT_DIR, PAUSE_SEC, load_dotenv, pick_topic

PROVIDERS = ("clova", "edge")

# --- Clova ---
CLOVA_URL = "https://naveropenapi.apigw.ntruss.com/tts-premium/v1/tts"
ENV_ID = "NCP_CLOVA_CLIENT_ID"
ENV_SECRET = "NCP_CLOVA_CLIENT_SECRET"
DEFAULT_SPEAKER = "nara"     # 어르신 대상: 또렷하고 차분한 목소리. 환경변수 CLOVA_SPEAKER 로 변경
DEFAULT_SPEED = 1            # -5(빠름)~5(느림). 0.85배속에 가깝게 1로 시작해 들어보고 조정
MAX_TEXT_CHARS = 2000        # Clova 요청당 글자 제한

# --- Edge (edge-tts) ---
DEFAULT_EDGE_VOICE = "ko-KR-SunHiNeural"   # 여성. 남성은 ko-KR-InJoonNeural. 환경변수 EDGE_VOICE 로 변경
DEFAULT_EDGE_RATE = "-15%"                 # 0.85배속. 환경변수 EDGE_RATE 로 변경


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


def plan_requests(script: dict, provider: str, params: dict) -> list[dict]:
    """문장 하나 = 요청 하나. 백엔드에 보낼 파라미터를 미리 만든다."""
    reqs = []
    for i, sent in enumerate(script["sentences"], 1):
        text = sent.strip()
        if len(text) > MAX_TEXT_CHARS:
            raise ValueError(f"{i}번 문장이 {MAX_TEXT_CHARS}자를 넘습니다.")
        if provider == "clova":
            form = {"speaker": params["speaker"], "speed": params["speed"], "volume": 0, "pitch": 0,
                    "format": "mp3", "text": text}
        else:
            form = {"voice": params["voice"], "rate": params["rate"], "text": text}
        reqs.append({"index": i, "file": f"{i:02d}.mp3", "form": form})
    return reqs


def check_provider_ready(provider: str) -> None:
    """실제 합성 전에 키·패키지를 확인한다. 문제가 있으면 안내하고 종료."""
    if provider == "clova":
        if not (os.environ.get(ENV_ID) and os.environ.get(ENV_SECRET)):
            sys.exit(f"{ENV_ID} / {ENV_SECRET} 환경변수가 없습니다. 파일에 키를 쓰지 말고 환경변수로 넣으세요.\n"
                     f"키 없이 쓰려면:  TTS_PROVIDER=edge python 02_tts.py")
    else:
        try:
            import edge_tts  # noqa: F401
        except ImportError:
            sys.exit("edge-tts 패키지가 없습니다:  pip install edge-tts")


def synthesize_clova(form: dict, out: Path) -> None:
    import requests

    r = requests.post(
        CLOVA_URL,
        headers={"X-NCP-APIGW-API-KEY-ID": os.environ[ENV_ID], "X-NCP-APIGW-API-KEY": os.environ[ENV_SECRET],
                 "Content-Type": "application/x-www-form-urlencoded"},
        data=form, timeout=60,
    )
    if r.status_code != 200:
        sys.exit(f"Clova 오류 {r.status_code}: {r.text[:300]}")
    out.write_bytes(r.content)


def synthesize_edge(form: dict, out: Path) -> None:
    """edge-tts: 키 없음. HTTPS_PROXY 가 있으면 그대로 태운다."""
    import asyncio
    import edge_tts

    async def run() -> None:
        comm = edge_tts.Communicate(form["text"], form["voice"], rate=form["rate"],
                                    proxy=os.environ.get("HTTPS_PROXY") or None)
        await comm.save(str(out))

    try:
        asyncio.run(run())
    except Exception as e:  # 네트워크·정책 오류를 한 줄로
        sys.exit(f"edge-tts 실패 ({type(e).__name__}): {str(e)[:200]}\n"
                 f"인터넷 연결과 프록시 정책(speech.platform.bing.com)을 확인하세요.")
    if not out.exists() or out.stat().st_size < 1000:
        sys.exit(f"edge-tts 결과가 비어 있습니다: {out}")


def synthesize(provider: str, form: dict, out: Path) -> None:
    (synthesize_clova if provider == "clova" else synthesize_edge)(form, out)


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
    ap.add_argument("--provider", choices=PROVIDERS, default=None,
                    help="TTS 백엔드 (기본: 환경변수 TTS_PROVIDER, 없으면 clova)")
    ap.add_argument("--speaker", default=None, help=f"[clova] 화자 (기본 {DEFAULT_SPEAKER}, 환경변수 CLOVA_SPEAKER)")
    ap.add_argument("--speed", type=int, default=DEFAULT_SPEED, help="[clova] -5(빠름)~5(느림), 기본 1")
    ap.add_argument("--voice", default=None, help=f"[edge] 음성 (기본 {DEFAULT_EDGE_VOICE}, 환경변수 EDGE_VOICE)")
    ap.add_argument("--rate", default=None, help=f"[edge] 속도 (기본 {DEFAULT_EDGE_RATE} = 0.85배속, 환경변수 EDGE_RATE)")
    ap.add_argument("--dry-run", action="store_true", help="호출 없이 요청 내용만 출력")
    ap.add_argument("--force", action="store_true", help="기존 narration.mp3 를 덮어쓴다")
    args = ap.parse_args()

    if not -5 <= args.speed <= 5:
        ap.error("--speed 는 -5~5 사이여야 합니다.")

    load_dotenv()   # .env 를 먼저 읽어야 TTS_PROVIDER 등이 보인다
    provider = (args.provider or os.environ.get("TTS_PROVIDER", "clova")).strip().lower()
    if provider not in PROVIDERS:
        sys.exit(f"TTS_PROVIDER={provider!r} 는 지원하지 않습니다. 가능: {', '.join(PROVIDERS)}")
    params = {
        "speaker": args.speaker or os.environ.get("CLOVA_SPEAKER", DEFAULT_SPEAKER),
        "speed": args.speed,
        "voice": args.voice or os.environ.get("EDGE_VOICE", DEFAULT_EDGE_VOICE),
        "rate": args.rate or os.environ.get("EDGE_RATE", DEFAULT_EDGE_RATE),
    }
    if provider == "edge" and not re.fullmatch(r"[+-]\d{1,3}%", params["rate"]):
        sys.exit(f"--rate 는 '+0%', '-15%' 형식이어야 합니다: {params['rate']!r}")

    try:
        script, script_path = load_script(args.id, args.row, args.script)
    except (FileNotFoundError, ValueError) as e:
        sys.exit(str(e))
    work_dir = script_path.parent
    reqs = plan_requests(script, provider, params)
    total_chars = sum(len(r["form"]["text"]) for r in reqs)
    voice_desc = (f"speaker={params['speaker']} speed={params['speed']}" if provider == "clova"
                  else f"voice={params['voice']} rate={params['rate']}")

    if args.dry_run:
        print(f"=== dry-run: {script_path} ===")
        print(f"provider : {provider}   {voice_desc}   pause: {PAUSE_SEC}s   format: mp3")
        if provider == "clova":
            print(f"endpoint : {CLOVA_URL}")
            print(f"headers  : X-NCP-APIGW-API-KEY-ID, X-NCP-APIGW-API-KEY  (환경변수 {ENV_ID}/{ENV_SECRET})")
            print(f"키 상태  : {'있음' if os.environ.get(ENV_ID) and os.environ.get(ENV_SECRET) else '없음'}")
        else:
            try:
                import edge_tts  # noqa: F401
                pkg = "설치됨"
            except ImportError:
                pkg = "없음 — pip install edge-tts"
            print(f"endpoint : Microsoft Edge TTS (edge-tts 패키지, 키 필요 없음, 무료)   패키지: {pkg}")
        for r in reqs:
            print(f"  {r['file']}  ({len(r['form']['text']):2d}자)  {r['form']['text']}")
        cost = "Clova 과금 기준" if provider == "clova" else "무료"
        print(f"\n요청 {len(reqs)}회, 총 {total_chars}자 ({cost}). 출력 예정: {work_dir / 'narration.mp3'}, {work_dir / 'audio.json'}")
        have_ffmpeg = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
        print(f"ffmpeg/ffprobe: {'있음' if have_ffmpeg else '없음 — 실제 실행 전에 설치 필요'}")
        print("[dry-run] 합성을 호출하지 않았습니다.")
        return 0

    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        sys.exit("ffmpeg/ffprobe 가 필요합니다.")
    final = work_dir / "narration.mp3"
    if final.exists() and not args.force:
        print(f"이미 있음: {final} (--force 로 재생성)")
        return 0

    check_provider_ready(provider)
    tts_dir = work_dir / "tts"
    tts_dir.mkdir(parents=True, exist_ok=True)
    print(f"[{script.get('id', '?')}] {provider} {voice_desc}, {len(reqs)}문장 {total_chars}자")

    segments, t = [], 0.0
    for r in reqs:
        out = tts_dir / r["file"]
        synthesize(provider, r["form"], out)
        d = duration_sec(out)
        segments.append({"index": r["index"], "text": r["form"]["text"], "file": str(out.relative_to(work_dir)),
                         "start": round(t, 3), "end": round(t + d, 3), "duration": round(d, 3)})
        print(f"  {r['file']}  {d:5.2f}s  {r['form']['text']}")
        t += d + PAUSE_SEC
    total = t - PAUSE_SEC

    concat_with_pauses([tts_dir / r["file"] for r in reqs], final, PAUSE_SEC)
    audio = {
        "id": script.get("id"), "narration": final.name, "provider": provider,
        **({"speaker": params["speaker"], "speed": params["speed"]} if provider == "clova"
           else {"voice": params["voice"], "rate": params["rate"]}),
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
