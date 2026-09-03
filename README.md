# silvertown — 시니어 건강 숏츠 자동 생성

어르신 대상 40~50초 건강 지식 유튜브 숏츠를 주제 한 줄에서 영상 파일까지 자동으로 만든다.
규칙과 파이프라인은 [CLAUDE.md](CLAUDE.md) 참고.

## 구조

```
topics.csv            주제 목록 (id, topic, key_message, sources, status)
sources/              대본 근거 문서 (여기 없는 내용은 대본에 쓰지 않음)
templates/            프롬프트 템플릿
  script_prompt.md    01 대본 프롬프트
pipeline/common.py    경로, .env 로드, topics.csv 읽기
assets/music, fonts   배경음악, 자막 폰트
output/{id}/          중간 산출물 (script.json, 음성, 자막 …) — 커밋 금지
output/{id}.mp4       최종 영상 — 커밋 금지
01_write.py           대본 생성
02_tts.py             나레이션 음성 (Naver Clova Voice, 문장별 합성 → narration.mp3 + audio.json)
03_subtitle.py        (예정) 자막 타이밍
04_background.py      (예정) 배경 소재
05_render.py          (예정) 렌더
06_upload.py          (예정) 업로드 — 별도 명령으로만
```

## 준비

```bash
pip install -r requirements.txt
cp .env.example .env    # 키를 채운다. .env 는 커밋되지 않는다.
```

## 01 대본 생성

```bash
python 01_write.py --dry-run   # 프롬프트만 확인 (API 호출 없음)
python 01_write.py             # topics.csv 첫 행 → output/001/script.json
python 01_write.py --id 001 --force
```

생성된 대본은 문장 수·글자 수·존댓말·금지어·안전 문구·핵심 메시지 반복을 자동 검사하고,
실패하면 1회 재생성한다. 그래도 남는 문제는 종료 코드 1과 함께 출력된다.

## 02 TTS (Naver Clova Voice)

```bash
python 02_tts.py --dry-run          # 요청 내용만 확인 (API 호출 없음)
python 02_tts.py                    # output/001/narration.mp3 + audio.json
python 02_tts.py --speaker nminsang --speed 2
```

문장마다 따로 합성해 길이를 재고 0.5초 간격으로 이어 붙인다.
`audio.json` 의 문장별 start/end 가 03 자막 타이밍의 입력이 된다. ffmpeg/ffprobe 필요.
