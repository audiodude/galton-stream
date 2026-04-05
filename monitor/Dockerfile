FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY monitor/ /app/
COPY assets/backup.png /app/backup.png

CMD ["python3", "monitor.py"]
