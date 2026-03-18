#!/bin/bash

echo "=== Starting up ==="
echo "STREAM KEY SET: $([ -z "$YOUTUBE_STREAM_KEY" ] && echo 'NO' || echo 'YES')"
echo "STREAM KEY LENGTH: ${#YOUTUBE_STREAM_KEY}"

# Start HTTP keep-alive
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
       -f flv "rtmp://a.rtmp.youtube.com/live2/${STREAM_KEY}" \
       -loglevel verbose 2>&1

EXIT_CODE=$?
echo "=== ffmpeg exited with code $EXIT_CODE ==="
sleep 60
