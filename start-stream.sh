#!/bin/bash

echo "=== Starting up ==="
echo "STREAM KEY SET: $([ -z "$YOUTUBE_STREAM_KEY" ] && echo 'NO' || echo 'YES')"

# Start HTTP keep-alive and wait for it to be ready
python3 -m http.server ${PORT:-10000} &
sleep 2
echo "Keep-alive server started on port ${PORT:-10000}"

STREAM_KEY="${YOUTUBE_STREAM_KEY}"
if [ -z "$STREAM_KEY" ]; then
  echo "ERROR: YOUTUBE_STREAM_KEY not set!"
  sleep 60
  exit 1
fi

echo "=== Checking video file ==="
ls -lh /app/video.mp4 || { echo "ERROR: video.mp4 missing!"; sleep 60; exit 1; }

echo "=== Starting ffmpeg ==="
ffmpeg -re -stream_loop -1 -i /app/video.mp4 \
       -c copy \
       -f flv "rtmp://a.rtmp.youtube.com/live2/${STREAM_KEY}" 2>&1 || {
  echo "ffmpeg failed with code $?"
  sleep 60
  exit 1
}
