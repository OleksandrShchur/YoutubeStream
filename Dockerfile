FROM python:3.12-slim

# ffmpeg (includes ffprobe, which app.py uses to size the -stream_loop count)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# HF Spaces runs containers as uid 1000 - don't run ffmpeg/Flask as root
RUN useradd -m -u 1000 user
WORKDIR /app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=user app.py .
COPY --chown=user templates/ templates/

# Pre-bake the (already YouTube-compliant 4K) video into the image.
# video.mp4 must sit NEXT TO this Dockerfile (hugging_face/video.mp4).
RUN mkdir -p /app/data/video_library && chown -R user:user /app/data
COPY --chown=user video.mp4 /app/data/video_library/video.mp4

USER user
ENV PYTHONUNBUFFERED=1
EXPOSE 7860
CMD ["python3", "app.py"]
