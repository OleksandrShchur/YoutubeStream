#!/bin/bash
# removed set -e so errors are visible

echo "=== Starting up ==="
echo "STREAM KEY SET: $([ -z "$YOUTUBE_STREAM_KEY" ] && echo 'NO - THIS IS THE PROBLEM' || echo 'YES')"

python3 -m http.server ${PORT:-10000} > /dev/null 2>&1 &
echo "Keep-alive server started"

STREAM_KEY="${YOUTUBE_STREAM_KEY}"
if [ -z "$STREAM_KEY" ]; then
  echo "ERROR: YOUTUBE_STREAM_KEY not set! Set it in Render dashboard under Environment."
  # Sleep so Render keeps the container alive long enough to read logs
  sleep 30
  exit 1
fi

echo "=== Checking video file ==="
ls -lh /app/video.mp4

echo "=== Starting ffmpeg stream ==="
ffmpeg -re -stream_loop -1 -i /app/video.mp4 \
       -c copy \
       -f flv "rtmp://a.rtmp.youtube.com/live2/${STREAM_KEY}"

echo "ffmpeg exited with code $?"
sleep 30
