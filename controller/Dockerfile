FROM python:3.12-slim

# Install dependencies
RUN apt-get update && apt-get install -y \
    git \
    curl \
    docker.io \
    && rm -rf /var/lib/apt/lists/*

# Allow git to work with mounted volumes owned by different users
RUN git config --global --add safe.directory '*'

# Install age for encrypted snapshots (detect architecture)
RUN ARCH=$(dpkg --print-architecture) && \
    if [ "$ARCH" = "arm64" ]; then AGE_ARCH="linux-arm64"; else AGE_ARCH="linux-amd64"; fi && \
    curl -sL "https://github.com/FiloSottile/age/releases/download/v1.2.0/age-v1.2.0-${AGE_ARCH}.tar.gz" | \
    tar xz -C /usr/local/bin --strip-components=1 age/age age/age-keygen

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy controller code
COPY . .

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
