FROM ubuntu:24.04

RUN apt-get update && apt-get install -y ffmpeg python3 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY start-stream.sh /app/start-stream.sh
RUN chmod +x /app/start-stream.sh

# Your media files
COPY video.mp4 /app/video.mp4

CMD ["/app/start-stream.sh"]
