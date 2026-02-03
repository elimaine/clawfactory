# Cloudflare Tunnel Setup

This guide sets up secure remote access to ClawFactory via Cloudflare Zero Trust.

## Prerequisites

- A Cloudflare account (free tier works)
- A domain managed by Cloudflare (or use `*.cfargotunnel.com` for testing)

## Step 1: Create Zero Trust Account

1. Go to https://one.dash.cloudflare.com/
2. Sign up or log in
3. You'll get a team domain like `your-team.cloudflareaccess.com`

## Step 2: Create the Tunnel

### Option A: Via Dashboard (Recommended)

1. Go to **Zero Trust** → **Networks** → **Tunnels**
2. Click **Create a tunnel**
3. Name it `clawfactory`
4. Choose **Cloudflared** connector
5. Copy the tunnel token (starts with `eyJ...`)
6. Save it to your secrets file:
   ```bash
   echo "TUNNEL_TOKEN=eyJ..." > secrets/tunnel.env
   ```

### Option B: Via CLI

```bash
# Install cloudflared
brew install cloudflared

# Login (opens browser)
cloudflared tunnel login

# Create tunnel
cloudflared tunnel create clawfactory

# Get the tunnel token
cloudflared tunnel token clawfactory
# Copy this to secrets/tunnel.env as TUNNEL_TOKEN=...
```

## Step 3: Configure Public Hostnames

In the Cloudflare Dashboard, add these routes to your tunnel:

| Public hostname | Service | Description |
|-----------------|---------|-------------|
| `gateway.yourdomain.com` | `http://proxy:80` | Gateway UI |
| `controller.yourdomain.com` | `http://proxy:8080` | Controller API |

To add:
1. Go to **Tunnels** → **clawfactory** → **Public Hostname**
2. Click **Add a public hostname**
3. Enter subdomain, domain, and service URL
4. Repeat for each service

## Step 4: Add Access Policies (Authentication)

Protect your services with Cloudflare Access:

1. Go to **Zero Trust** → **Access** → **Applications**
2. Click **Add an application** → **Self-hosted**
3. Configure:
   - **Name**: ClawFactory Gateway
   - **Subdomain**: `gateway`
   - **Domain**: `yourdomain.com`
4. Add a policy:
   - **Name**: Allowed Users
   - **Action**: Allow
   - **Include**: Emails ending in `@yourdomain.com` (or specific emails)
5. Repeat for Controller

## Step 5: Start the Tunnel

```bash
# Start everything including tunnel
docker compose --profile tunnel up -d

# Or start tunnel separately
docker compose --profile tunnel up -d tunnel
```

## Step 6: Verify

1. Visit `https://gateway.yourdomain.com`
2. You should see Cloudflare Access login
3. After authenticating, you'll see the Gateway UI

## Troubleshooting

### Check tunnel status
```bash
docker logs clawfactory-tunnel
```

### Test from inside the network
```bash
docker exec clawfactory-tunnel curl http://proxy:80/health
```

### Verify tunnel is connected
In Cloudflare Dashboard: **Tunnels** → **clawfactory** should show "Healthy"

## Security Notes

- Tunnel token is sensitive - keep `secrets/tunnel.env` out of git
- Access policies control who can reach your services
- All traffic is encrypted (Cloudflare → your tunnel)
- Services never exposed directly to internet

## Disabling Remote Access

```bash
# Stop just the tunnel
docker compose --profile tunnel stop tunnel

# Or run without tunnel profile
docker compose up -d  # Only starts proxy, gateway, controller
```

## Additional: GitHub Webhooks via Tunnel

If you want GitHub to send webhooks to your controller:

1. Add public hostname: `webhooks.yourdomain.com` → `http://proxy:8080`
2. In Access, create a **Bypass** policy for `/webhook` path
3. Set GitHub webhook URL to `https://webhooks.yourdomain.com/webhook`

---

## Quick Reference

```bash
# Local only
docker compose up -d
# Access: http://localhost:18789 (gateway), http://localhost:8080 (controller)

# With remote access
docker compose --profile tunnel up -d
# Access: https://gateway.yourdomain.com, https://controller.yourdomain.com
```
