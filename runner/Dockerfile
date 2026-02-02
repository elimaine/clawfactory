FROM python:3.12-slim

# Install git and other tools
RUN apt-get update && apt-get install -y \
    git \
    curl \
    jq \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd -m -s /bin/bash runner
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy runner code
COPY runner.py .
COPY tools/ ./tools/

# Switch to non-root user
USER runner

# Run the socket server
CMD ["python", "runner.py"]
