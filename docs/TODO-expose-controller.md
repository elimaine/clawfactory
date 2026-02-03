# TODO: Expose Controller for GitHub Webhooks

The Controller needs a public URL for GitHub to send webhook events when PRs are merged.

## Options

### Option A: Cloudflare Tunnel (Recommended)

Zero-trust access without exposing ports.

```bash
# Install cloudflared
brew install cloudflared

# Authenticate
cloudflared tunnel login

# Create tunnel
cloudflared tunnel create clawfactory

# Configure tunnel (add to ~/.cloudflared/config.yml)
tunnel: <tunnel-id>
credentials-file: /Users/<you>/.cloudflared/<tunnel-id>.json

ingress:
  - hostname: clawfactory.yourdomain.com
    service: http://localhost:8080
  - service: http_status:404

# Run tunnel
cloudflared tunnel run clawfactory

# Or run as service
cloudflared service install
```

Webhook URL: `https://clawfactory.yourdomain.com/webhook/github`

### Option B: Tailscale Funnel

Expose to internet via Tailscale.

```bash
# Enable funnel (requires Tailscale account)
tailscale funnel 8080
```

Webhook URL: `https://<machine-name>.<tailnet>.ts.net/webhook/github`

### Option C: ngrok (Development only)

Quick testing, not for production.

```bash
ngrok http 8080
```

Webhook URL: `https://<random>.ngrok.io/webhook/github`

## After Exposing

1. Re-run `./install.sh`
2. Choose auto-configure GitHub → Y
3. Enter your public controller URL
4. Script will create webhook on your brain repository

Or manually add webhook at:
https://github.com/YOUR_USERNAME/{instance}-brain/settings/hooks/new

## Security Notes

- Controller only accepts webhooks signed with `github.webhook_secret`
- Only merges by users in `allowed_merge_actors` trigger promotion
- Consider IP allowlisting GitHub's webhook IPs if your proxy supports it
