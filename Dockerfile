FROM python:3.11-slim

# Install system deps: ffmpeg, ffprobe, aria2, yt-dlp, wget
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    aria2 \
    wget \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install latest yt-dlp binary
RUN curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
        -o /usr/local/bin/yt-dlp \
    && chmod +x /usr/local/bin/yt-dlp

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/downloads

CMD ["python", "main.py"]
