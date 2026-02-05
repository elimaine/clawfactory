# ClawFactory Gateway Dockerfile
# Extends OpenClaw with Docker-in-Docker support via Sysbox
#
# This Dockerfile is used when sandbox.mode is enabled.
# It installs Docker inside the container so OpenClaw can create
# sandbox containers for tool execution.
#
# Requires: Sysbox runtime on the host (runtime: sysbox-runc in compose)
#
# Build with:
#   docker build --build-arg OPENCLAW_IMAGE=clawfactory-gateway:local -t clawfactory-gateway-sandbox:local ./gateway

ARG OPENCLAW_IMAGE=clawfactory-gateway:local
FROM ${OPENCLAW_IMAGE}

USER root

# Install Docker CLI and daemon for sandbox container creation
# Sysbox allows this to run securely without privileged mode
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gnupg \
    lsb-release \
    iptables \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $(lsb_release -cs) stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        docker-ce \
        docker-ce-cli \
        containerd.io \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Create docker group and add node user to it
RUN groupadd -f docker && usermod -aG docker node

# Create directories for Docker daemon
RUN mkdir -p /var/run/docker /var/lib/docker

# Copy the sandbox entrypoint script
COPY sandbox-entrypoint.sh /usr/local/bin/sandbox-entrypoint.sh
RUN chmod +x /usr/local/bin/sandbox-entrypoint.sh

# Build the default OpenClaw sandbox image inside the container
# This will be done at first boot via the entrypoint

# Switch back to node user for runtime
# Note: dockerd will be started as root via the entrypoint
USER node

ENTRYPOINT ["/usr/local/bin/sandbox-entrypoint.sh"]
