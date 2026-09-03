# silvertown — 시니어 건강 숏츠 자동 생성

어르신 대상 40~50초 건강 지식 유튜브 숏츠를 주제 한 줄에서 영상 파일까지 자동으로 만든다.
규칙과 파이프라인은 [CLAUDE.md](CLAUDE.md) 참고.

## 구조

```
topics.csv            주제 목록 (id, topic, key_message, sources, status)
sources/              대본 근거 문서 (여기 없는 내용은 대본에 쓰지 않음)
templates/            프롬프트 템플릿
  script_prompt.md    01 대본 프롬프트
  image_style.md      배경 그림 고정 스타일·피할 것·문장별 image_prompt 규칙
pipeline/common.py    경로, .env 로드, topics.csv 읽기
assets/fonts/         자막 폰트 (tools/fetch_fonts.py 로 내려받음, FONT.json 이 family 이름을 기록)
assets/backgrounds/   배경 소재 (공통 또는 {id}/ 하위 폴더)
assets/music/         배경음악
tools/fetch_fonts.py  Pretendard Bold(없으면 NanumGothic Bold) + 라이선스 내려받기
tools/install_ffmpeg.sh  ffmpeg 설치 (apt / brew / winget / conda)
output/{id}/          중간 산출물 (script.json, 음성, 자막 …) — 커밋 금지
output/{id}.mp4       최종 영상 — 커밋 금지
01_write.py           대본 생성
02_tts.py             나레이션 음성 (Naver Clova Voice, 문장별 합성 → narration.mp3 + audio.json)
03_subtitle.py        자막 줄나눔·타이밍 (subtitles.json / .srt / .ass)
04_background.py      배경 소재 배정 (로컬 → Pexels → 그라데이션)
05_render.py          ffmpeg 렌더 → output/{id}.mp4 (1080x1920 30fps, 자막 번인, 배경음악 -20dB)
06_upload.py          YouTube 업로드(기본 private) — 별도 명령으로만, 이 환경에서는 실행 안 함
```

## 준비

```bash
sudo apt install ffmpeg        # 05 렌더, 02 TTS 이어붙이기
pip install -r requirements.txt
cp .env.example .env         # 키를 채운다. .env 는 커밋되지 않는다.
python tools/fetch_fonts.py  # 자막 폰트 (Pretendard Bold, SIL OFL) 내려받기
```

## 01 대본 생성

```bash
python 01_write.py --dry-run   # 프롬프트만 확인 (API 호출 없음)
python 01_write.py             # topics.csv 첫 행 → output/001/script.json
python 01_write.py --id 001 --force
```

생성된 대본은 문장 수·글자 수·존댓말·금지어·안전 문구·핵심 메시지 반복을 자동 검사하고,
실패하면 1회 재생성한다. 그래도 남는 문제는 종료 코드 1과 함께 출력된다.

## 02 TTS (Naver Clova Voice 또는 edge-tts)

```bash
python 02_tts.py --dry-run                    # 요청 내용만 확인 (호출 없음)
python 02_tts.py                              # clova (기본): output/001/narration.mp3 + audio.json
TTS_PROVIDER=edge python 02_tts.py            # edge-tts: 키 없이, 무료
python 02_tts.py --provider edge --voice ko-KR-InJoonNeural --rate -15%
python 02_tts.py --provider clova --speaker nminsang --speed 2
```

`TTS_PROVIDER` 는 `.env` 에 넣어도 된다. edge 는 `--rate -15%` 로 0.85배속을 정확히 맞추고, clova 는 정수 단계라 들어보고 조정한다.

문장마다 따로 합성해 길이를 재고 0.5초 간격으로 이어 붙인다.
`audio.json` 의 문장별 start/end 가 03 자막 타이밍의 입력이 된다. ffmpeg/ffprobe 필요.

## 03 자막

```bash
python 03_subtitle.py --estimate   # audio.json 없이 글자 수로 시간 추정
python 03_subtitle.py              # audio.json 실측 타이밍 사용
```

한 줄 10자(공백 제외), 한 화면 3줄, 쉼표 우선 줄바꿈. `.ass` 는 1080x1920, 90px 흰 글자 + 검은 테두리.

## 04 배경 소재

```bash
python 04_background.py --dry-run  # 컷 계획만
python 04_background.py            # background.json 생성
```

문장 경계에서만 컷을 바꾸고 한 컷은 3초 이상. 소재는 `assets/backgrounds/{id}/` → `assets/backgrounds/` → 이미지 생성(`IMAGE_PROVIDER=fal`, `FAL_KEY` 필요) → Pexels(`USE_PEXELS=true`) → 그라데이션 PNG 순서로 고른다.

```bash
IMAGE_PROVIDER=fal python 04_background.py --dry-run   # 컷별 프롬프트만 확인 (무료)
IMAGE_PROVIDER=fal python 04_background.py             # 컷당 한 장 생성 → output/{id}/bg_{n}.png (과금)
```

프롬프트는 01 이 문장마다 만든 `image_prompts` 에 `templates/image_style.md` 의 style 과 avoid 를 붙인 것이다. 키가 없으면 그라데이션으로 넘어간다.

## 05 렌더

```bash
python 05_render.py --silent       # 나레이션 없이 화면만 확인
python 05_render.py                # narration.mp3 + assets/music/ 첫 파일 → output/001.mp4
python 05_render.py --dry-run      # ffmpeg 명령만 출력
```

컷마다 1080x1920 으로 채우고(가로 소재는 가운데 crop), 이미지 컷은 천천히 확대. 자막은 `.ass` 를 번인.
배경음악은 -20dB 로 낮춰 섞고 끝에서 2초 페이드아웃. 렌더 후 `preview_*.jpg` 세 장과 `render.json` 을 남긴다.

## 한 편 전체 흐름

```bash
python 01_write.py && python 02_tts.py && python 03_subtitle.py && python 04_background.py && python 05_render.py
```

## 06 업로드 (별도 실행, 기본 비공개)

```bash
python 06_upload.py --id 001 --dry-run   # 제목·설명·해시태그 확인
python 06_upload.py --id 001             # private 업로드, 확인 질문 있음
```

OAuth 토큰은 `token.json` 에서만 읽는다. 없으면 개인 기기에서 `python 06_upload.py --auth client_secret.json` 으로 만든다. `token.json`, `client_secret*.json` 은 커밋하지 않는다.

## 다음 세션 체크리스트

1. **환경변수 (세션 시작 전에 환경 설정에 넣을 것)**
   - `ANTHROPIC_API_KEY` — 01 대본 생성
   - `NCP_CLOVA_CLIENT_ID`, `NCP_CLOVA_CLIENT_SECRET` — 02 TTS (네이버 클라우드 > AI·NAVER API > CLOVA Voice)
   - 키가 아직 없으면 `TTS_PROVIDER=edge` 로 먼저 돌려볼 수 있다 (무료, 인터넷 필요)
   - 확인: `python -c "import os;print([k for k in ('ANTHROPIC_API_KEY','NCP_CLOVA_CLIENT_ID','NCP_CLOVA_CLIENT_SECRET') if os.environ.get(k)])"`
2. **도구 설치**
   ```bash
   bash tools/install_ffmpeg.sh
   pip install -r requirements.txt
   python tools/fetch_fonts.py
   ```
3. **근거 문서 채우기** — `sources/*.md` 의 `[채워야 함]` 을 실제 출처(질병관리청 등 공식 자료 URL, 확인일)와 내용으로 바꾼다.
   표시가 남아 있으면 01 이 대본을 만들지 않는다. 001 의 `dehydration_summer.md` 도 예시 출처를 실제 출처로 바꿀 것.
4. **첫 실행 순서 (topics.csv 첫 행, 비용 드는 단계는 실행 전 확인)**
   ```bash
   python 01_write.py --dry-run      # 프롬프트 확인 (무료)
   python 01_write.py                # Claude API 1~2회
   python 02_tts.py --dry-run        # 요청 내용 확인 (무료)
   python 02_tts.py                  # Clova Voice, 문장 수만큼 호출
   python 03_subtitle.py             # audio.json 실측 타이밍
   python 04_background.py           # 소재 없으면 그라데이션
   python 05_render.py               # output/001.mp4 + preview_*.jpg
   ```
   02 결과의 `audio.json` 총 길이를 보고 `pipeline/common.py` 의 `CHARS_PER_SEC`(현재 4.2 추정) 를 보정한다.
5. **세션 종료 전** — `output/` 을 비운다 (`rm -rf output/*`). 남길 미리보기는 `docs/preview/{id}/` 로 복사.
