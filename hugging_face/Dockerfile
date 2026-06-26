FROM python:3.10-slim

# Install ffmpeg
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app source
COPY app.py .
COPY templates/ templates/

# Pre-bake video into the image (avoids need for persistent disk on free tier)
COPY video.mp4 /app/data/video_library/video.mp4

# Hugging Face Spaces requires port 7860
EXPOSE 7860

CMD ["python3", "app.py"]
