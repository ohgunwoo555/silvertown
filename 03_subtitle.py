#!/usr/bin/env python3
"""03 자막 타이밍: script.json + audio.json → subtitles.json / .srt / .ass

02 가 만든 audio.json 의 문장별 start/end 를 그대로 쓴다. 자막 한 줄은 10자(공백 제외),
한 화면 최대 3줄. 문장이 그보다 길면 글자 수 비율로 시간을 나눠 두 화면으로 쪼갠다.

사용법:
    python 03_subtitle.py                 # topics.csv 첫 행
    python 03_subtitle.py --id 001
    python 03_subtitle.py --estimate      # audio.json 없이 글자 수로 시간 추정 (TTS 전 미리보기)
    python 03_subtitle.py --script 경로.json --estimate

출력: output/{id}/subtitles.json (05 렌더 입력), subtitles.srt (확인용), subtitles.ass (스타일 포함)
외부 API 호출 없음.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from pipeline.common import (
    ASSETS_DIR,
    CHARS_PER_SEC,
    OUTPUT_DIR,
    PAUSE_SEC,
    SUBTITLE_FONT_PX,
    SUBTITLE_LINE_CHARS,
    SUBTITLE_MAX_LINES,
    pick_topic,
)

MAX_LINES = SUBTITLE_MAX_LINES
MIN_CUE_SEC = 1.2      # 이보다 짧은 자막은 읽기 어렵다 → 경고
GAP_SEC = 0.08         # 다음 자막과의 최소 간격 (문장 사이 무음 동안 자막을 유지)
FONT_DIR = ASSETS_DIR / "fonts"
FONT_FALLBACK = "Pretendard"   # FONT.json 이 없을 때 (tools/fetch_fonts.py 로 받는다)


def load_font() -> tuple[str, str | None]:
    """assets/fonts/FONT.json → (family, 파일 경로). 05 렌더가 fontsdir 로 넘긴다."""
    meta = FONT_DIR / "FONT.json"
    if meta.exists():
        m = json.loads(meta.read_text(encoding="utf-8"))
        path = FONT_DIR / m["file"]
        if path.exists():
            return m["family"], str(path)
        print(f"⚠ FONT.json 은 있지만 {path.name} 이 없습니다. python tools/fetch_fonts.py 를 실행하세요.")
        return m["family"], None
    print("⚠ assets/fonts/FONT.json 이 없습니다. python tools/fetch_fonts.py 를 실행하세요.")
    return FONT_FALLBACK, None


# ---------- 줄 나누기 ----------

def vis(s: str) -> int:
    """공백을 뺀 글자 수. 한 줄 10자 규칙은 이 기준으로 센다."""
    return len(re.sub(r"\s", "", s))


NBSP = "\u00a0"
# 다음 어절과 떨어지면 뜻이 끊기는 한 글자 부사. 줄바꿈 계산에서 뒤 어절과 한 덩어리로 다룬다.
GLUE_WORDS = ("안", "못", "잘", "더", "좀", "꼭", "딱", "다", "또", "늘")


def _glue(text: str) -> str:
    return re.sub(r"(?<!\S)(" + "|".join(GLUE_WORDS) + r") (?=\S)", r"\1" + NBSP, text)


def _greedy(text: str, width: int) -> list[str]:
    lines, cur = [], ""
    for word in _glue(text).split(" "):
        while vis(word) > width:            # 한 어절이 한 줄보다 길면 강제 분할
            if cur:
                lines.append(cur); cur = ""
            lines.append(word[:width]); word = word[width:]
        cand = f"{cur} {word}".strip()
        if vis(cand) <= width:
            cur = cand
        else:
            lines.append(cur); cur = word
    if cur:
        lines.append(cur)
    return [l.replace(NBSP, " ") for l in lines]


def wrap_line(text: str, width: int = SUBTITLE_LINE_CHARS) -> list[str]:
    """어절 단위로 width 자(공백 제외) 이내 줄을 만든다.

    후보 두 가지 — (a) 쉼표 앞뒤를 따로 줄바꿈, (b) 어절 그리디 — 중 줄 수가 적은 쪽을 고르고,
    같으면 쉼표 쪽을 택한다. ("땀이 안 / 나면" 처럼 붙어 있어야 할 말이 갈라지는 것을 줄인다)
    """
    text = re.sub(r"\s+", " ", text.strip())
    if vis(text) <= width:
        return [text]
    greedy = _greedy(text, width)
    if "," not in text:
        return greedy
    parts = [x.strip() for x in text.split(",")]
    by_comma: list[str] = []
    for i, part in enumerate(parts):
        if not part:
            continue
        if i < len(parts) - 1:
            part += ","
        by_comma += _greedy(part, width)
    return by_comma if len(by_comma) <= len(greedy) else greedy


def balance_two_lines(lines: list[str], width: int = SUBTITLE_LINE_CHARS) -> list[str]:
    """2줄일 때 위아래 길이가 너무 다르면 어절을 옮겨 균형을 맞춘다."""
    if len(lines) != 2:
        return lines
    words = (lines[0] + " " + lines[1]).split(" ")
    best, best_diff = lines, abs(vis(lines[0]) - vis(lines[1]))
    for k in range(1, len(words)):
        a, b = " ".join(words[:k]), " ".join(words[k:])
        if vis(a) <= width and vis(b) <= width:
            d = abs(vis(a) - vis(b))
            # 쉼표 뒤에서 끊는 것을 우선한다
            if a.endswith(",") and d <= best_diff + 2 or d < best_diff:
                best, best_diff = [a, b], d
    return best


def strip_end_punct(line: str) -> str:
    """자막에서는 문장 끝 마침표를 뺀다. 물음표·쉼표는 남긴다."""
    return re.sub(r"\.$", "", line)


# ---------- 타이밍 ----------

def estimate_segments(sentences: list[str]) -> list[dict]:
    """audio.json 이 없을 때: 글자 수 / CHARS_PER_SEC 로 추정."""
    segs, t = [], 0.0
    for i, s in enumerate(sentences, 1):
        d = max(1.0, vis(s) / CHARS_PER_SEC)
        segs.append({"index": i, "text": s, "start": round(t, 3), "end": round(t + d, 3), "duration": round(d, 3)})
        t += d + PAUSE_SEC
    return segs


def build_cues(segments: list[dict]) -> tuple[list[dict], list[str]]:
    cues, warnings = [], []
    for k, seg in enumerate(segments):
        lines = [strip_end_punct(l) for l in balance_two_lines(wrap_line(seg["text"]))]
        # 다음 문장 시작 직전까지 자막을 유지 (무음 구간에 화면이 비지 않게)
        next_start = segments[k + 1]["start"] if k + 1 < len(segments) else seg["end"] + PAUSE_SEC
        end = max(seg["end"], next_start - GAP_SEC)

        # 2줄을 넘으면 글자 수 비율로 시간을 나눠 여러 화면으로
        chunks = [lines[i:i + MAX_LINES] for i in range(0, len(lines), MAX_LINES)]
        if len(chunks) > 1:
            warnings.append(f"{seg['index']}번 문장이 {len(lines)}줄 → {len(chunks)}화면으로 나눔: {seg['text']}")
        total_chars = sum(len("".join(c)) for c in chunks) or 1
        t, span = seg["start"], end - seg["start"]
        for j, chunk in enumerate(chunks):
            share = len("".join(chunk)) / total_chars
            c_end = end if j == len(chunks) - 1 else t + span * share
            cue = {"sentence": seg["index"], "start": round(t, 3), "end": round(c_end, 3), "lines": chunk}
            if c_end - t < MIN_CUE_SEC:
                warnings.append(f"{seg['index']}번 자막이 {c_end - t:.2f}초로 짧습니다: {' / '.join(chunk)}")
            cues.append(cue)
            t = c_end
    return cues, warnings


# ---------- 파일 형식 ----------

def fmt_srt(t: float) -> str:
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def fmt_ass(t: float) -> str:
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def to_srt(cues: list[dict]) -> str:
    out = []
    for n, c in enumerate(cues, 1):
        out.append(f"{n}\n{fmt_srt(c['start'])} --> {fmt_srt(c['end'])}\n" + "\n".join(c["lines"]) + "\n")
    return "\n".join(out)


def to_ass(cues: list[dict], font: str, size: int = SUBTITLE_FONT_PX) -> str:
    # 1080x1920 세로. 흰 글자 + 검은 테두리, 화면 중앙 아래쪽(유튜브 UI 를 피해 MarginV 로 올림).
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Senior,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,0,2,40,40,520,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    nl = "\\N"
    lines = [f"Dialogue: 0,{fmt_ass(c['start'])},{fmt_ass(c['end'])},Senior,,0,0,0,,{nl.join(c['lines'])}" for c in cues]
    return header + "\n".join(lines) + "\n"


# ---------- main ----------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--id", help="topics.csv 의 id")
    g.add_argument("--row", type=int, help="topics.csv 행 번호 (1부터)")
    ap.add_argument("--script", help="script.json 경로를 직접 지정")
    ap.add_argument("--estimate", action="store_true", help="audio.json 없이 글자 수로 시간 추정")
    args = ap.parse_args()

    try:
        script_path = Path(args.script) if args.script else pick_topic(args.id, args.row).script_path
        if not script_path.exists():
            raise FileNotFoundError(f"대본이 없습니다: {script_path}  (먼저 01_write.py)")
        script = json.loads(script_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError) as e:
        sys.exit(str(e))
    work_dir = script_path.parent
    audio_path = work_dir / "audio.json"

    if audio_path.exists() and not args.estimate:
        audio = json.loads(audio_path.read_text(encoding="utf-8"))
        segments, timing = audio["segments"], "audio.json (TTS 실측)"
        if len(segments) != len(script["sentences"]):
            sys.exit(f"audio.json 문장 수({len(segments)})와 script.json({len(script['sentences'])})이 다릅니다. 02 를 다시 실행하세요.")
    elif args.estimate or not audio_path.exists():
        if not args.estimate:
            print(f"audio.json 이 없어 글자 수로 추정합니다 (--estimate). 02 실행 후 다시 돌리면 실측으로 바뀝니다.")
        segments, timing = estimate_segments(script["sentences"]), f"추정 ({CHARS_PER_SEC}자/초 + 문장 사이 {PAUSE_SEC}초)"

    cues, warnings = build_cues(segments)
    total = cues[-1]["end"] if cues else 0.0
    font_family, font_file = load_font()
    result = {
        "id": script.get("id"), "timing": timing, "total_sec": round(total, 3),
        "font": font_family, "font_file": font_file, "font_px": SUBTITLE_FONT_PX, "line_chars": SUBTITLE_LINE_CHARS,
        "cues": cues, "warnings": warnings,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (work_dir / "subtitles.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (work_dir / "subtitles.srt").write_text(to_srt(cues), encoding="utf-8")
    (work_dir / "subtitles.ass").write_text(to_ass(cues, font_family), encoding="utf-8")

    print(f"[{script.get('id')}] 타이밍: {timing} / 폰트: {font_family}")
    for c in cues:
        print(f"  {c['start']:6.2f} → {c['end']:6.2f}  {' / '.join(c['lines'])}")
    print(f"\n자막 {len(cues)}개, 총 {total:.1f}초 → {work_dir.relative_to(OUTPUT_DIR.parent)}/subtitles.{{json,srt,ass}}")
    for w in warnings:
        print(f"⚠ {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
