#!/bin/bash
set -e

echo "=== Starting up ==="
echo "PORT: ${PORT:-10000}"
echo "STREAM KEY SET: $([ -z "$YOUTUBE_STREAM_KEY" ] && echo 'NO' || echo 'YES')"

python3 -m http.server ${PORT:-10000} > /dev/null 2>&1 &
echo "Keep-alive server started on port ${PORT:-10000}"

STREAM_KEY="${YOUTUBE_STREAM_KEY}"
if [ -z "$STREAM_KEY" ]; then
  echo "ERROR: YOUTUBE_STREAM_KEY not set!"
  exit 1
fi

echo "=== Checking video file ==="
ls -lh /app/video.mp4 || echo "ERROR: video.mp4 not found!"
ffprobe /app/video.mp4 2>&1 | head -20

echo "=== Starting stream ==="
ffmpeg -re -stream_loop -1 -i /app/video.mp4 \
       -c copy \
       -f flv "rtmp://a.rtmp.youtube.com/live2/${STREAM_KEY}" 2>&1

echo "=== ffmpeg exited with code $? ==="
