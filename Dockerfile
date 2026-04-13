FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# Install dependencies
RUN apt-get update && apt-get install -y \
    wget \
    unzip \
    xvfb \
    ffmpeg \
    libgl1-mesa-dri \
    libgl1-mesa-glx \
    libegl1-mesa \
    libgles2-mesa \
    libxcursor1 \
    libxinerama1 \
    libxrandr2 \
    libxi6 \
    libasound2 \
    libpulse0 \
    libfontconfig1 \
    libdbus-1-3 \
    unclutter \
    x11-xserver-utils \
    awscli \
    curl \
    procps \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

# chat_poller uses gRPC streaming for YouTube live chat (the REST polling
# endpoint burns the 10k/day API quota in a few hours).
RUN pip3 install --no-cache-dir grpcio==1.80.0 protobuf==6.33.6

# Install Godot 4.4
RUN ARCH=$(dpkg --print-architecture) && \
    if [ "$ARCH" = "arm64" ]; then \
        GODOT_FILE="Godot_v4.4-stable_linux.arm64"; \
    else \
        GODOT_FILE="Godot_v4.4-stable_linux.x86_64"; \
    fi && \
    wget -q "https://github.com/godotengine/godot/releases/download/4.4-stable/${GODOT_FILE}.zip" \
    && unzip "${GODOT_FILE}.zip" \
    && mv "$GODOT_FILE" /usr/local/bin/godot \
    && chmod +x /usr/local/bin/godot \
    && rm "${GODOT_FILE}.zip"

# Copy project
WORKDIR /app
COPY . /app

COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

CMD ["/app/start.sh"]
