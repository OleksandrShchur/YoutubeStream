FROM ubuntu:24.04

RUN apt-get update && apt-get install -y ffmpeg python3 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY start-stream.sh /app/start-stream.sh
RUN chmod +x /app/start-stream.sh

COPY video.mp4 /tmp/video_raw.mp4
RUN ffmpeg -i /tmp/video_raw.mp4 \
       -c:v libx264 -preset slow \
       -b:v 4500k -maxrate 6000k -bufsize 12000k \
       -vf 'scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2' \
       -pix_fmt yuv420p -r 30 -g 60 -keyint_min 60 \
       -c:a aac -b:a 128k -ar 44100 \
       /app/video.mp4 && \
    rm /tmp/video_raw.mp4

CMD ["/app/start-stream.sh"]
