FROM python:3.11-slim

RUN pip3 install --no-cache-dir boto3

WORKDIR /app

COPY monitor/ /app/
COPY assets/radio-offline.html /app/radio-offline.html

CMD ["python3", "monitor.py"]
