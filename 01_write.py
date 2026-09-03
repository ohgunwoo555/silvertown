#!/usr/bin/env python3
"""01 대본 생성: topics.csv 한 행 + sources/ 근거 → output/{id}/script.json

사용법:
    python 01_write.py                 # topics.csv 첫 행
    python 01_write.py --id 001        # id 지정
    python 01_write.py --row 3         # 3번째 행
    python 01_write.py --dry-run       # API 호출 없이 프롬프트만 출력 (무료)
    python 01_write.py --force         # 이미 script.json 이 있어도 다시 생성

비용: Claude API 호출 1~2회 (검증 실패 시 1회 재시도). 실행 전 확인할 것.
키: ANTHROPIC_API_KEY 환경변수(또는 .env)에서만 읽는다.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone

from pipeline.common import (
    CHARS_PER_SEC,
    OUTPUT_DIR,
    PAUSE_SEC,
    TEMPLATES_DIR,
    Topic,
    estimate_seconds,
    load_dotenv,
    pick_topic,
    read_sources,
)

MODEL = "claude-opus-5"
TEMPLATE = TEMPLATES_DIR / "script_prompt.md"

# 40~50초 / 0.85배속 기준. CHARS_PER_SEC(4.2자/초)와 문장 사이 PAUSE_SEC(0.5초)로 계산하면
# 문장 11개일 때 40초 ≈ 147자, 50초 ≈ 189자. 문장당 15자 → 10~12문장 안팎.
MIN_SENTENCES = 9
MAX_SENTENCES = 13
MAX_CHARS_PER_SENTENCE = 20   # "15자 내외"의 상한 (공백 제외)
MIN_TOTAL_CHARS = 140   # 공백 제외
MAX_TOTAL_CHARS = 190

SAFETY_WORDS = ("병원", "의사", "진료")
BANNED_PATTERNS = [
    r"영양제", r"건강기능식품", r"보충제", r"이온음료", r"처방", r"복용",
    r"[A-Za-z]{3,}",           # 영어 단어
    r"\d+\s*(mg|ml|㎎|㎖|kcal)",  # 단위 붙은 수치
]

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "sentences": {"type": "array", "items": {"type": "string"}},
        "keywords": {"type": "array", "items": {"type": "string"}},
        "description": {"type": "string"},
    },
    "required": ["title", "sentences", "keywords", "description"],
    "additionalProperties": False,
}


# ---------- 프롬프트 ----------

def load_template() -> tuple[str, str]:
    """templates/script_prompt.md 를 (system, user) 로 나눈다."""
    text = TEMPLATE.read_text(encoding="utf-8")
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    m = re.search(r"^## system\s*$(.*?)^## user\s*$(.*)", text, flags=re.S | re.M)
    if not m:
        raise ValueError(f"{TEMPLATE} 에 '## system' / '## user' 구획이 없습니다.")
    return m.group(1).strip(), m.group(2).strip()


def render(text: str, values: dict[str, str]) -> str:
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", str(v))
    leftover = re.findall(r"\{\{(\w+)\}\}", text)
    if leftover:
        raise ValueError(f"치환되지 않은 자리표시자: {leftover}")
    return text


def build_prompt(topic: Topic) -> tuple[str, str]:
    system_t, user_t = load_template()
    values = {
        "topic": topic.topic,
        "key_message": topic.key_message,
        "sources": read_sources(topic),
        "min_sentences": MIN_SENTENCES,
        "max_sentences": MAX_SENTENCES,
        "max_chars": MAX_CHARS_PER_SENTENCE,
    }
    return render(system_t, values), render(user_t, values)


# ---------- 검증 ----------

def validate(script: dict, topic: Topic) -> list[str]:
    """CLAUDE.md 대본 규칙 중 기계로 잡을 수 있는 것만 검사. 문제 목록을 돌려준다."""
    problems: list[str] = []
    sents = [s.strip() for s in script.get("sentences", []) if s and s.strip()]
    if not sents:
        return ["문장이 없습니다."]

    def nchars(s: str) -> int:  # 공백 제외 글자 수 (TTS 길이는 음절 수를 따른다)
        return len(re.sub(r"\s", "", s))

    n = len(sents)
    if not MIN_SENTENCES <= n <= MAX_SENTENCES:
        problems.append(f"문장 수 {n}개 (허용 {MIN_SENTENCES}~{MAX_SENTENCES})")

    total = sum(nchars(s) for s in sents)
    if not MIN_TOTAL_CHARS <= total <= MAX_TOTAL_CHARS:
        problems.append(f"총 글자 수 {total}자 (허용 {MIN_TOTAL_CHARS}~{MAX_TOTAL_CHARS}, 40~50초 기준)")

    for i, s in enumerate(sents, 1):
        if nchars(s) > MAX_CHARS_PER_SENTENCE:
            problems.append(f"{i}번 문장 {nchars(s)}자, {MAX_CHARS_PER_SENTENCE}자 초과: {s}")
        if not re.search(r"(요|다|까|죠)[.!?]?$", s):
            problems.append(f"{i}번 문장이 존댓말 종결이 아닙니다: {s}")
        for pat in BANNED_PATTERNS:
            if re.search(pat, s):
                problems.append(f"{i}번 문장에 금지 표현({pat}): {s}")
                break

    joined = " ".join(sents)
    if "어르신" not in joined:
        problems.append("'어르신' 호칭이 없습니다.")
    if not any(w in joined for w in SAFETY_WORDS):
        problems.append("안전 문구(병원/의사/진료)가 없습니다.")

    # 마지막 문장 = 핵심 메시지 반복 (공백·구두점 제외 후 절반 이상 겹치면 통과)
    def norm(s: str) -> set[str]:
        return set(re.sub(r"[\s.,!?~]", "", s))
    km, last = norm(topic.key_message), norm(sents[-1])
    if km and len(km & last) / len(km) < 0.5:
        problems.append(f"마지막 문장이 핵심 메시지와 다릅니다: {sents[-1]}")

    title = script.get("title", "")
    if not title or len(title) > 20:
        problems.append(f"제목이 비었거나 20자 초과: {title!r}")
    if not 2 <= len(script.get("keywords", [])) <= 4:
        problems.append("keywords 는 2~4개여야 합니다.")
    return problems


# ---------- API ----------

def call_claude(system: str, user: str, feedback: str | None = None) -> tuple[dict, dict]:
    import anthropic

    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": user}]
    if feedback:
        messages.append({"role": "user", "content": feedback})

    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=8000,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
            output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
            # 안전 분류기가 거절하면 같은 요청을 대체 모델로 서버에서 다시 실행
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.AuthenticationError:
        sys.exit("ANTHROPIC_API_KEY 가 없거나 잘못되었습니다. 환경변수 또는 .env 를 확인하세요.")
    except anthropic.RateLimitError as e:
        sys.exit(f"요청 한도 초과. {e.response.headers.get('retry-after', '?')}초 후 다시 시도하세요.")
    except anthropic.APIStatusError as e:
        sys.exit(f"API 오류 {e.status_code}: {e.message}")
    except anthropic.APIConnectionError:
        sys.exit("네트워크 오류로 API 에 연결하지 못했습니다.")

    if response.stop_reason == "refusal":
        detail = getattr(response, "stop_details", None)
        sys.exit(f"모델이 요청을 거절했습니다: {getattr(detail, 'explanation', '')}")
    if response.stop_reason == "max_tokens":
        sys.exit("응답이 잘렸습니다(max_tokens). 프롬프트를 확인하세요.")

    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        sys.exit(f"JSON 파싱 실패: {e}\n---\n{text}")

    usage = {
        "model": response.model,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", 0),
    }
    return data, usage


def feedback_message(problems: list[str]) -> str:
    lines = "\n".join(f"- {p}" for p in problems)
    return (
        "방금 쓴 대본에 아래 문제가 있습니다. 규칙을 다시 확인하고 전체 JSON 을 다시 쓰세요.\n"
        f"{lines}"
    )


# ---------- main ----------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--id", help="topics.csv 의 id")
    g.add_argument("--row", type=int, help="topics.csv 행 번호 (1부터)")
    ap.add_argument("--dry-run", action="store_true", help="API 호출 없이 프롬프트만 출력")
    ap.add_argument("--force", action="store_true", help="기존 script.json 을 덮어쓴다")
    args = ap.parse_args()

    load_dotenv()
    try:
        topic = pick_topic(args.id, args.row)
        system, user = build_prompt(topic)
    except (FileNotFoundError, ValueError) as e:
        sys.exit(str(e))

    if args.dry_run:
        print("=== SYSTEM ===\n" + system + "\n\n=== USER ===\n" + user)
        print(f"\n[dry-run] {topic.id} 프롬프트 {len(system) + len(user)}자. API 는 호출하지 않았습니다.")
        return 0

    if topic.script_path.exists() and not args.force:
        print(f"[{topic.id}] 이미 있음: {topic.script_path} (--force 로 재생성)")
        return 0

    print(f"[{topic.id}] {topic.topic} → 대본 생성 ({MODEL})")
    script, usage = call_claude(system, user)
    problems = validate(script, topic)
    attempts = 1
    if problems:
        print("검증 실패, 1회 재시도:\n  " + "\n  ".join(problems))
        # 첫 응답을 assistant 턴으로 돌려주고 수정 요청
        retry_user = user
        retry_feedback = (
            "이전 답변:\n" + json.dumps(script, ensure_ascii=False) + "\n\n" + feedback_message(problems)
        )
        script, usage2 = call_claude(system, retry_user, retry_feedback)
        usage["output_tokens"] += usage2["output_tokens"]
        usage["input_tokens"] += usage2["input_tokens"]
        problems = validate(script, topic)
        attempts = 2

    sents = [s.strip() for s in script["sentences"]]
    total = sum(len(re.sub(r"\s", "", s)) for s in sents)
    record = {
        "id": topic.id,
        "topic": topic.topic,
        "key_message": topic.key_message,
        "sources": topic.sources,
        "title": script["title"],
        "sentences": sents,
        "keywords": script["keywords"],
        "description": script["description"],
        "stats": {"sentences": len(sents), "chars": total, "est_seconds": estimate_seconds(sents)},
        "validation": {"ok": not problems, "problems": problems, "attempts": attempts},
        "model": usage["model"],
        "usage": usage,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    topic.work_dir.mkdir(parents=True, exist_ok=True)
    topic.script_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n제목: {record['title']}")
    for i, s in enumerate(sents, 1):
        print(f"{i:2d}. {s}")
    print(f"\n{len(sents)}문장 / {total}자 / 약 {record['stats']['est_seconds']}초  "
          f"(입력 {usage['input_tokens']} 출력 {usage['output_tokens']} 토큰)")
    print(f"저장: {topic.script_path.relative_to(OUTPUT_DIR.parent)}")
    if problems:
        print("\n⚠ 재시도 후에도 남은 문제 (손으로 고치거나 --force 로 재생성):\n  " + "\n  ".join(problems))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
