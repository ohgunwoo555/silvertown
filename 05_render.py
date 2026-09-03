#!/usr/bin/env python3
"""05 렌더: background.json + subtitles.ass + narration.mp3 (+ 배경음악) → output/{id}.mp4

1080x1920 30fps, 자막은 .ass 그대로(흰 글자·검은 테두리·Pretendard), 배경음악은 -20dB.
이미지 컷에는 천천히 확대되는 움직임을 준다(--no-motion 으로 끔).

사용법:
    python 05_render.py                     # topics.csv 첫 행
    python 05_render.py --id 001
    python 05_render.py --silent            # narration.mp3 없이 무음으로 렌더 (TTS 전 화면 확인용)
    python 05_render.py --narration a.mp3 --music b.mp3   # 파일 직접 지정
    python 05_render.py --dry-run           # ffmpeg 명령만 출력

배경음악: --music 경로 > assets/music/ 의 첫 파일 > 없으면 나레이션만.
외부 API 호출 없음. ffmpeg/ffprobe 필요.
"""
from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from pipeline.common import ASSETS_DIR, OUTPUT_DIR, pick_topic

W, H, FPS = 1080, 1920, 30
MUSIC_DB = -20.0          # CLAUDE.md: 배경음악 -20dB 이하
MUSIC_FADE_SEC = 2.0
CRF = 20
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac"}
ZOOM_TO = 1.08            # 이미지 컷 끝에서의 확대 배율


def probe_duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                         capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def probe_video(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                          "-show_entries", "stream=width,height,r_frame_rate,codec_name:format=duration",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    j = json.loads(out)
    s = j["streams"][0]
    num, den = s["r_frame_rate"].split("/")
    return {"width": s["width"], "height": s["height"], "fps": round(int(num) / int(den), 2),
            "codec": s["codec_name"], "duration": round(float(j["format"]["duration"]), 2)}


def ff_escape(path: str) -> str:
    """필터 옵션 안의 경로: ':' 와 '\\' 와 "'" 를 이스케이프."""
    return path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def find_music(explicit: str | None) -> Path | None:
    if explicit:
        p = Path(explicit)
        if not p.exists():
            sys.exit(f"배경음악이 없습니다: {p}")
        return p
    music_dir = ASSETS_DIR / "music"
    if music_dir.is_dir():
        files = sorted(p for p in music_dir.iterdir() if p.suffix.lower() in AUDIO_EXTS)
        if files:
            return files[0]
    return None


def build_command(bg: dict, ass_path: Path, fonts_dir: Path | None, narration: Path | None,
                  music: Path | None, out_path: Path, motion: bool) -> list[str]:
    total = float(bg["total_sec"])
    cuts = bg["cuts"]
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-stats"]
    filters: list[str] = []

    # --- 배경 컷 입력 ---
    for i, c in enumerate(cuts):
        dur, f = float(c["duration"]), Path(c["file"])
        if not f.exists():
            sys.exit(f"컷 {c['index']} 소재가 없습니다: {f}")
        if c["type"] == "image":
            cmd += ["-loop", "1", "-framerate", str(FPS), "-t", f"{dur:.3f}", "-i", str(f)]
        else:
            cmd += ["-stream_loop", "-1", "-t", f"{dur:.3f}", "-i", str(f)]   # 짧은 영상은 반복
        frames = max(1, round(dur * FPS))
        chain = (f"[{i}:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
                 f"crop={W}:{H},setsar=1,fps={FPS},format=yuv420p")
        if c["type"] == "image" and motion:
            # 컷 길이에 걸쳐 1.0 → ZOOM_TO 로 천천히 확대. 가운데 고정.
            chain += (f",zoompan=z='1+({ZOOM_TO}-1)*on/{frames}':d=1:s={W}x{H}:fps={FPS}"
                      f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'")
        chain += f",trim=duration={dur:.3f},setpts=PTS-STARTPTS[v{i}]"
        filters.append(chain)
    n = len(cuts)
    filters.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[vcat]")

    # --- 자막 ---
    ass_opt = f"ass='{ff_escape(str(ass_path))}'"
    if fonts_dir:
        ass_opt += f":fontsdir='{ff_escape(str(fonts_dir))}'"
    filters.append(f"[vcat]{ass_opt}[vout]")

    # --- 오디오 ---
    a_idx = n
    if narration:
        cmd += ["-i", str(narration)]
    else:
        cmd += ["-f", "lavfi", "-t", f"{total:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
    filters.append(f"[{a_idx}:a]aformat=sample_rates=48000:channel_layouts=stereo,apad=whole_dur={total:.3f}[nar]")
    if music:
        cmd += ["-stream_loop", "-1", "-t", f"{total:.3f}", "-i", str(music)]
        fade_st = max(0.0, total - MUSIC_FADE_SEC)
        filters.append(f"[{a_idx + 1}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                       f"volume={MUSIC_DB}dB,atrim=duration={total:.3f},"
                       f"afade=t=in:st=0:d=1,afade=t=out:st={fade_st:.3f}:d={MUSIC_FADE_SEC}[mus]")
        filters.append("[nar][mus]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]")
    else:
        filters.append("[nar]acopy[aout]")

    cmd += ["-filter_complex", ";".join(filters), "-map", "[vout]", "-map", "[aout]",
            "-t", f"{total:.3f}", "-r", str(FPS),
            "-c:v", "libx264", "-preset", "medium", "-crf", str(CRF), "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out_path)]
    return cmd


def extract_previews(video: Path, total: float, work_dir: Path) -> list[Path]:
    outs = []
    for tag, t in (("start", 1.0), ("mid", total / 2), ("end", max(0.5, total - 1.5))):
        p = work_dir / f"preview_{tag}.jpg"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(video),
                        "-frames:v", "1", "-q:v", "3", str(p)], check=True)
        outs.append(p)
    return outs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--id", help="topics.csv 의 id")
    g.add_argument("--row", type=int, help="topics.csv 행 번호 (1부터)")
    ap.add_argument("--narration", help="나레이션 파일 (기본 output/{id}/narration.mp3)")
    ap.add_argument("--music", help="배경음악 파일 (기본 assets/music/ 첫 파일)")
    ap.add_argument("--silent", action="store_true", help="나레이션 없이 무음으로 렌더")
    ap.add_argument("--no-motion", action="store_true", help="이미지 컷 확대 움직임 끄기")
    ap.add_argument("--dry-run", action="store_true", help="ffmpeg 명령만 출력")
    ap.add_argument("--force", action="store_true", help="기존 mp4 덮어쓰기")
    args = ap.parse_args()

    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        sys.exit("ffmpeg/ffprobe 가 필요합니다.")
    try:
        topic = pick_topic(args.id, args.row)
    except (FileNotFoundError, ValueError) as e:
        sys.exit(str(e))
    work_dir = topic.work_dir
    bg_path, subs_path, ass_path = work_dir / "background.json", work_dir / "subtitles.json", work_dir / "subtitles.ass"
    for p, step in ((bg_path, "04_background.py"), (subs_path, "03_subtitle.py"), (ass_path, "03_subtitle.py")):
        if not p.exists():
            sys.exit(f"{p.name} 이 없습니다. 먼저 {step} 를 실행하세요.")
    bg = json.loads(bg_path.read_text(encoding="utf-8"))
    subs = json.loads(subs_path.read_text(encoding="utf-8"))

    fonts_dir = Path(subs["font_file"]).parent if subs.get("font_file") else None
    if not fonts_dir:
        print("⚠ 폰트 파일이 없어 시스템 폰트로 대체됩니다. python tools/fetch_fonts.py 를 실행하세요.")

    narration: Path | None
    if args.silent:
        narration = None
    else:
        narration = Path(args.narration) if args.narration else work_dir / "narration.mp3"
        if not narration.exists():
            sys.exit(f"나레이션이 없습니다: {narration}  (02_tts.py 를 먼저 실행하거나 --silent)")
        nd = probe_duration(narration)
        if abs(nd - float(bg["total_sec"])) > 1.0:
            print(f"⚠ 나레이션 {nd:.1f}초와 컷 계획 {bg['total_sec']:.1f}초가 다릅니다. 03·04 를 다시 실행했는지 확인하세요.")
    music = find_music(args.music)

    out_path = OUTPUT_DIR / f"{topic.id}.mp4"
    cmd = build_command(bg, ass_path, fonts_dir, narration, music, out_path, motion=not args.no_motion)

    print(f"[{topic.id}] 컷 {len(bg['cuts'])}개({bg['source']}) / 자막 {len(subs['cues'])}개 / "
          f"나레이션 {narration.name if narration else '무음'} / 배경음악 {music.name + f' ({MUSIC_DB:+.0f}dB)' if music else '없음'}")
    if args.dry_run:
        print(shlex.join(cmd))
        return 0
    if out_path.exists() and not args.force:
        print(f"이미 있음: {out_path} (--force 로 다시 렌더)")
        return 0

    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"ffmpeg 실패:\n{r.stderr[-2000:]}")

    info = probe_video(out_path)
    previews = extract_previews(out_path, float(bg["total_sec"]), work_dir)
    (work_dir / "render.json").write_text(json.dumps({
        "id": topic.id, "output": str(out_path), **info,
        "narration": str(narration) if narration else None, "music": str(music) if music else None,
        "music_db": MUSIC_DB if music else None, "previews": [str(p) for p in previews],
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    ok = info["width"] == W and info["height"] == H and abs(info["fps"] - FPS) < 0.01
    print(f"→ {out_path.relative_to(OUTPUT_DIR.parent)}  {info['width']}x{info['height']} {info['fps']}fps "
          f"{info['duration']}s {info['codec']}  {'OK' if ok else '⚠ 규격 불일치'}")
    print("미리보기: " + ", ".join(p.name for p in previews))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
