FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir boto3

WORKDIR /app

COPY monitor/ /app/
COPY assets/backup.png /app/backup.png
COPY assets/radio-offline.html /app/radio-offline.html

CMD ["python3", "monitor.py"]
