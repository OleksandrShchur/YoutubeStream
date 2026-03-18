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

ffmpeg -re -stream_loop -1 -r 10 -i /app/video.mp4 \
       -c:v libx264 -preset ultrafast -tune zerolatency \
       -b:v 800k -maxrate 1000k -bufsize 2000k \
       -vf 'scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2' \
       -pix_fmt yuv420p -r 10 -g 20 -keyint_min 20 \
       -c:a aac -b:a 128k -ar 44100 \
       -f flv "rtmp://a.rtmp.youtube.com/live2/${STREAM_KEY}"
