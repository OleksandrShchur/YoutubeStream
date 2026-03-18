#!/bin/bash
set -e

python3 -m http.server ${PORT:-10000} > /dev/null 2>&1 &
echo "Keep-alive server started on port ${PORT:-10000}"

STREAM_KEY="${YOUTUBE_STREAM_KEY}"
if [ -z "$STREAM_KEY" ]; then
  echo "ERROR: YOUTUBE_STREAM_KEY not set!"
  exit 1
fi

ffmpeg -re -stream_loop -1 -i /app/video.mp4 \
       -c copy \
       -f flv "rtmp://a.rtmp.youtube.com/live2/${STREAM_KEY}"
