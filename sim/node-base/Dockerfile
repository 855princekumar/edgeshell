# EdgeShell Simulated Edge Node
# Mimics a Raspberry Pi / industrial Linux edge device
# - Full SSH server (OpenSSH)
# - systemd-like process management via supervisord
# - Docker-in-Docker (simulates Docker running on the node)
# - Node-RED (simulates IoT middleware)
# - Realistic /proc metrics (CPU/RAM/disk readable via SSH)
# - Ansible-compatible (Python3 + sudo)

FROM ubuntu:22.04

ARG NODE_NAME=edge-node
ARG NODE_USER=pi
ARG NODE_PASSWORD=raspberry

ENV DEBIAN_FRONTEND=noninteractive
ENV NODE_NAME=${NODE_NAME}
ENV NODE_USER=${NODE_USER}

# Install everything a real Pi would have
RUN apt-get update && apt-get install -y --no-install-recommends \
    # SSH
    openssh-server \
    # Python (Ansible requirement)
    python3 python3-pip \
    # Utilities EdgeShell will run
    htop curl wget net-tools procps \
    iproute2 dnsutils lsof \
    # Simulate workloads
    stress-ng \
    # Supervisor (keeps sshd + fake services running)
    supervisor \
    # Node-RED deps
    nodejs npm \
    # Misc
    sudo vim nano jq bc \
    && rm -rf /var/lib/apt/lists/*

# Install Node-RED globally
RUN npm install -g --unsafe-perm node-red@3.1.0 2>/dev/null || \
    npm install -g --unsafe-perm node-red 2>/dev/null || true

# Create the edge node user (mimics pi user on Raspberry Pi)
RUN useradd -m -s /bin/bash ${NODE_USER} \
    && echo "${NODE_USER}:${NODE_PASSWORD}" | chpasswd \
    && usermod -aG sudo ${NODE_USER} \
    && echo "${NODE_USER} ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

# SSH setup
RUN mkdir -p /var/run/sshd \
    && sed -i 's/#PermitRootLogin prohibit-password/PermitRootLogin no/' /etc/ssh/sshd_config \
    && sed -i 's/#PasswordAuthentication yes/PasswordAuthentication yes/' /etc/ssh/sshd_config \
    && sed -i 's/#PubkeyAuthentication yes/PubkeyAuthentication yes/' /etc/ssh/sshd_config \
    && sed -i 's/UsePAM yes/UsePAM no/' /etc/ssh/sshd_config \
    && echo "AllowUsers ${NODE_USER}" >> /etc/ssh/sshd_config

# Supervisor config: manages sshd, node-red and fake-docker-stats
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Boot scripts
COPY scripts/entrypoint.sh /entrypoint.sh
COPY scripts/fake-docker-stats.sh /usr/local/bin/fake-docker-stats
COPY scripts/fake-metrics.sh /usr/local/bin/fake-metrics
COPY scripts/node-info.sh /usr/local/bin/node-info

RUN chmod +x /entrypoint.sh /usr/local/bin/fake-docker-stats \
    /usr/local/bin/fake-metrics /usr/local/bin/node-info

EXPOSE 22 1880

ENTRYPOINT ["/entrypoint.sh"]
