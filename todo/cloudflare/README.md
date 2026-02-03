# Cloudflare Tunnel Setup (TODO)

This folder contains everything needed to enable Cloudflare Zero Trust tunnel for remote access to ClawFactory.

## Prerequisites

1. A Cloudflare account
2. A domain managed by Cloudflare (added to your Cloudflare account)

## To Enable Cloudflare Tunnel

### 1. Add tunnel service to docker-compose.yml

Add this service block to `docker-compose.yml` (after the proxy service):

```yaml
  # ============================================================
  # Tunnel: Cloudflare Zero Trust tunnel (OPTIONAL)
  # - Enable with: docker compose --profile tunnel up -d
  # - Requires TUNNEL_TOKEN in secrets/tunnel.env
  # - See todo/cloudflare/ for setup instructions
  # ============================================================
  tunnel:
    image: cloudflare/cloudflared:latest
    container_name: clawfactory-tunnel
    restart: unless-stopped
    profiles:
      - tunnel
    command: tunnel run
    env_file:
      - ./secrets/tunnel.env
    networks:
      - clawfactory
    depends_on:
      - proxy
```

### 2. Create secrets/tunnel.env

```bash
cp todo/cloudflare/tunnel.env.example secrets/tunnel.env
# Edit secrets/tunnel.env with your tunnel token
```

### 3. Add start-tunnel command to clawfactory.sh

Add this case to the script:

```bash
    start-tunnel)
        ${COMPOSE_CMD} --profile tunnel up -d
        echo "✓ ClawFactory started with Cloudflare tunnel"
        ;;
```

### 4. Follow setup guide

See `cloudflare-tunnel-setup.md` in this folder for:
- Creating a tunnel in Cloudflare dashboard
- Getting your tunnel token
- Configuring public hostnames
- Adding Access policies for authentication

## Files in this folder

- `README.md` - This file
- `cloudflare-tunnel-setup.md` - Full setup guide
- `tunnel.env.example` - Template for tunnel token
