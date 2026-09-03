#!/usr/bin/env python3
"""06 업로드: output/{id}.mp4 → YouTube (기본 비공개 private)

이 단계는 파이프라인에 묶지 않고 별도 명령으로만 실행한다. 이 개발 환경에서는 실행하지 않는다.

제목·설명·해시태그는 output/{id}/script.json (title, key_message, description, topic) 에서 만든다.
OAuth 토큰은 프로젝트 루트의 token.json 에서만 읽는다 (없으면 안내 후 종료, 이 스크립트는 토큰을 만들거나 쓰지 않는다).

token.json 만들기 (개인 기기에서 한 번):
    1. Google Cloud Console → YouTube Data API v3 사용 설정 → OAuth 클라이언트(데스크톱 앱) 생성 → client_secret.json 내려받기
    2. 개인 기기에서:  python 06_upload.py --auth client_secret.json   (브라우저가 열리고 token.json 이 저장됨)
    3. token.json 은 절대 커밋하지 않는다 (.gitignore 에 있음)

사용법:
    python 06_upload.py --id 001 --dry-run     # 메타데이터와 파일만 확인, 업로드 안 함
    python 06_upload.py --id 001               # 비공개로 업로드 (확인 질문 있음)
    python 06_upload.py --id 001 --privacy unlisted --yes

필요 패키지: google-api-python-client google-auth google-auth-oauthlib (requirements.txt)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from pipeline.common import OUTPUT_DIR, ROOT, pick_topic

TOKEN_PATH = ROOT / "token.json"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
CATEGORY_ID = "26"           # Howto & Style
DEFAULT_PRIVACY = "private"
BASE_HASHTAGS = ["어르신건강", "시니어건강", "건강상식", "Shorts"]
DISCLAIMER = ("이 영상은 일반적인 건강 정보이며 진단이나 치료를 대신하지 않습니다. "
              "몸이 불편하시면 병원에 가서 의사와 상의하세요.")


# ---------- 메타데이터 ----------

def hashtag(text: str) -> str:
    """공백·기호를 뺀 해시태그. 예: '어르신 여름철 탈수 예방' → '#어르신여름철탈수예방'"""
    return "#" + re.sub(r"[^\w가-힣]", "", text)


def build_metadata(script: dict) -> dict:
    title = (script.get("title") or script.get("key_message") or script.get("topic") or "").strip()
    key_message = (script.get("key_message") or "").strip()
    topic = (script.get("topic") or "").strip()
    body = (script.get("description") or "").strip()

    tags_text = BASE_HASHTAGS + ([topic] if topic else [])
    hashtags = " ".join(hashtag(t) for t in tags_text)

    description = "\n\n".join(x for x in [
        body,
        f"오늘의 한 가지: {key_message}" if key_message else "",
        DISCLAIMER,
        hashtags,
    ] if x)

    yt_title = title if "#Shorts" in title else f"{title} #Shorts"
    if len(yt_title) > 100:                 # YouTube 제목 제한
        yt_title = yt_title[:97] + "..."

    tags = [t for t in ["어르신 건강", "시니어 건강", "건강 상식", topic, key_message] if t]
    return {
        "snippet": {
            "title": yt_title,
            "description": description[:5000],   # YouTube 설명 제한
            "tags": tags[:15],
            "categoryId": CATEGORY_ID,
            "defaultLanguage": "ko",
            "defaultAudioLanguage": "ko",
        },
        "status": {
            "privacyStatus": DEFAULT_PRIVACY,
            "selfDeclaredMadeForKids": False,
        },
    }


# ---------- 인증 ----------

def load_credentials():
    """token.json 만 읽는다. 없으면 안내하고 종료. 파일에 쓰지 않는다."""
    if not TOKEN_PATH.exists():
        sys.exit(
            "token.json 이 없습니다. 업로드는 개인 기기에서 인증한 뒤 실행하세요.\n"
            "  개인 기기에서:  python 06_upload.py --auth client_secret.json\n"
            "  (브라우저 로그인 후 token.json 이 저장됩니다. 이 파일은 커밋하지 않습니다.)"
        )
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError:
        sys.exit("google-auth 패키지가 없습니다:  pip install google-api-python-client google-auth google-auth-oauthlib")
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())          # 메모리에서만 갱신, token.json 은 그대로 둔다
    if not creds.valid:
        sys.exit("token.json 이 만료되었거나 유효하지 않습니다. 개인 기기에서 다시 인증하세요 (--auth).")
    return creds


def run_auth(client_secret: str) -> int:
    """개인 기기 전용: 브라우저 로그인으로 token.json 생성. 개발 환경에서는 쓰지 않는다."""
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        sys.exit("google-auth-oauthlib 패키지가 없습니다:  pip install google-auth-oauthlib")
    if TOKEN_PATH.exists():
        sys.exit(f"{TOKEN_PATH.name} 이 이미 있습니다. 다시 인증하려면 먼저 지우세요.")
    flow = InstalledAppFlow.from_client_secrets_file(client_secret, SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    print(f"저장: {TOKEN_PATH}  (커밋 금지)")
    return 0


# ---------- 업로드 ----------

def upload(creds, video: Path, body: dict) -> dict:
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    youtube = build("youtube", "v3", credentials=creds)
    media = MediaFileUpload(str(video), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    try:
        while response is None:
            status, response = request.next_chunk()
            if status:
                print(f"  업로드 {int(status.progress() * 100)}%", end="\r", flush=True)
    except HttpError as e:
        sys.exit(f"YouTube API 오류 {e.resp.status}: {e.content[:300]!r}")
    print()
    return response


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--id", help="topics.csv 의 id")
    g.add_argument("--row", type=int, help="topics.csv 행 번호 (1부터)")
    ap.add_argument("--privacy", choices=["private", "unlisted", "public"], default=DEFAULT_PRIVACY,
                    help=f"공개 범위 (기본 {DEFAULT_PRIVACY})")
    ap.add_argument("--dry-run", action="store_true", help="메타데이터만 출력, 업로드 안 함")
    ap.add_argument("--yes", action="store_true", help="확인 질문 없이 업로드")
    ap.add_argument("--auth", metavar="CLIENT_SECRET_JSON", help="(개인 기기 전용) 브라우저 인증으로 token.json 생성")
    args = ap.parse_args()

    if args.auth:
        return run_auth(args.auth)

    try:
        topic = pick_topic(args.id, args.row)
    except (FileNotFoundError, ValueError) as e:
        sys.exit(str(e))
    video = OUTPUT_DIR / f"{topic.id}.mp4"
    if not topic.script_path.exists():
        sys.exit(f"script.json 이 없습니다: {topic.script_path}")
    if not video.exists():
        sys.exit(f"영상이 없습니다: {video}  (먼저 05_render.py)")
    script = json.loads(topic.script_path.read_text(encoding="utf-8"))
    body = build_metadata(script)
    body["status"]["privacyStatus"] = args.privacy

    size_mb = video.stat().st_size / 1024 / 1024
    print(f"[{topic.id}] {video.name} ({size_mb:.1f} MB) → YouTube [{args.privacy}]")
    print(f"제목: {body['snippet']['title']}")
    print("설명:\n" + "\n".join("  " + l for l in body["snippet"]["description"].splitlines()))
    print(f"태그: {', '.join(body['snippet']['tags'])}")
    print(f"token.json: {'있음' if TOKEN_PATH.exists() else '없음 (개인 기기에서 --auth 로 만들 것)'}")

    if args.dry_run:
        print("[dry-run] 업로드하지 않았습니다.")
        return 0

    creds = load_credentials()
    if not args.yes:
        answer = input(f"{args.privacy} 로 업로드할까요? [y/N] ").strip().lower()
        if answer != "y":
            print("취소했습니다.")
            return 1

    resp = upload(creds, video, body)
    video_id = resp.get("id")
    url = f"https://youtu.be/{video_id}" if video_id else None
    record = {"id": topic.id, "video_id": video_id, "url": url, "privacy": args.privacy,
              "title": body["snippet"]["title"], "uploaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    (topic.work_dir / "upload.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"완료: {url}  ({args.privacy})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
