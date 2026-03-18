#!/bin/bash
set -e

# === KEEP-ALIVE: tiny HTTP server (prevents free-tier spin-down) ===
python3 -m http.server ${PORT:-10000} > /dev/null 2>&1 &

echo "Keep-alive server started on port ${PORT:-10000}"

STREAM_KEY="${YOUTUBE_STREAM_KEY}"
if [ -z "$STREAM_KEY" ]; then
  echo "ERROR: YOUTUBE_STREAM_KEY not set!"
  exit 1
fi

echo "Starting 1080p YouTube stream (WARNING: may stutter or suspend soon)..."

ffmpeg -re -stream_loop -1 -i /app/video.mp4 \
       -c:v libx264 -preset veryfast -tune zerolatency \
       -b:v 1500k -maxrate 2000k -bufsize 4000k \
       -vf scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2 \
       -pix_fmt yuv420p -r 30 -g 60 -keyint_min 60 \
       -c:a aac -b:a 128k -ar 44100 \
       -f flv "rtmp://a.rtmp.youtube.com/live2/${STREAM_KEY}"
