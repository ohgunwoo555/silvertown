#!/usr/bin/env bash
# ffmpeg/ffprobe 설치 (02 TTS 이어붙이기, 05 렌더에 필요). 이미 있으면 아무것도 하지 않는다.
set -e
if command -v ffmpeg >/dev/null && command -v ffprobe >/dev/null; then
  echo "ffmpeg 있음: $(ffmpeg -version | head -1)"; exit 0
fi
if command -v apt-get >/dev/null; then
  SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
  $SUDO apt-get update -q && $SUDO apt-get install -y -q ffmpeg
elif command -v brew >/dev/null; then
  brew install ffmpeg
elif command -v winget >/dev/null; then
  winget install --id Gyan.FFmpeg -e
elif command -v conda >/dev/null; then
  conda install -y -c conda-forge ffmpeg
else
  echo "패키지 관리자를 찾지 못했습니다. https://ffmpeg.org/download.html 에서 직접 설치하세요."; exit 1
fi
ffmpeg -version | head -1
