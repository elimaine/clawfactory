# TODO: Cloudflare Zero Trust Egress

Lock down Gateway container's outbound access using Cloudflare Zero Trust.

## Goal

Instead of allowing all outbound traffic, route through Cloudflare and allowlist only:
- `discord.com` / `discord.gg` / `gateway.discord.gg`
- `api.anthropic.com`
- `api.github.com` (if needed)

## Option A: WARP Client in Sidecar

Run Cloudflare WARP as a sidecar container, route Gateway traffic through it.

```yaml
services:
  warp:
    image: cloudflare/cloudflare-warp:latest
    container_name: clawfactory-warp
    cap_add:
      - NET_ADMIN
    volumes:
      - warp-data:/var/lib/cloudflare-warp
    environment:
      - WARP_LICENSE_KEY=${CF_WARP_LICENSE_KEY}
    networks:
      - clawfactory

  gateway:
    # ... existing config ...
    network_mode: "service:warp"  # Route through WARP
    depends_on:
      - warp
```

## Option B: Gateway Egress Policies

Use Cloudflare Gateway (part of Zero Trust) to create egress policies:

1. Create a Zero Trust account at https://one.dash.cloudflare.com/
2. Go to Gateway → Policies → Network
3. Create policy:
   - Allow: `discord.com`, `*.discord.com`, `*.discord.gg`
   - Allow: `api.anthropic.com`
   - Block: Everything else

4. Configure WARP client with your team domain

## Option C: Docker Network Egress Rules

Use iptables on the host to restrict container egress:

```bash
# Get container IP range
docker network inspect clawfactory | jq '.[0].IPAM.Config[0].Subnet'

# Allow only specific destinations
iptables -I DOCKER-USER -s 172.18.0.0/16 -d discord.com -j ACCEPT
iptables -I DOCKER-USER -s 172.18.0.0/16 -d api.anthropic.com -j ACCEPT
iptables -I DOCKER-USER -s 172.18.0.0/16 -j DROP
```

## Resources

- https://developers.cloudflare.com/cloudflare-one/connections/connect-devices/warp/
- https://developers.cloudflare.com/cloudflare-one/policies/gateway/
- https://docs.docker.com/network/packet-filtering-firewalls/
