FROM python:3.10-slim

# Install system dependencies including FFmpeg
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy the app script and video asset into the container
COPY app.py /app/app.py
COPY video.mp4 /app/video.mp4

# Run the python entrypoint script
CMD ["python3", "app.py"]
