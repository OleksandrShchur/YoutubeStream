#!/bin/bash
set -e

# === KEEP-ALIVE: tiny HTTP server on Render's $PORT (prevents spin-down) ===
python3 -m http.server ${PORT:-10000} > /dev/null 2>&1 &

echo "Keep-alive server started on port ${PORT:-10000}"

STREAM_KEY="${YOUTUBE_STREAM_KEY}"
if [ -z "$STREAM_KEY" ]; then
  echo "ERROR: YOUTUBE_STREAM_KEY not set!"
  exit 1
fi

echo "Starting YouTube live stream..."

ffmpeg -loop 1 -i /app/image.jpg \
       -stream_loop -1 -i /app/audio.mp3 \
       -c:v libx264 -preset ultrafast -tune stillimage \
       -b:v 200k -maxrate 300k -bufsize 600k \
       -pix_fmt yuv420p -g 60 \
       -c:a aac -b:a 64k -ar 44100 \
       -f flv "rtmp://a.rtmp.youtube.com/live2/${STREAM_KEY}"
