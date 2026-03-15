# Instant Health Data Upload via Tailscale

Upload health export CSVs directly from your phone to your computer for instant import — no waiting for iCloud sync.

## How It Works

```
iPhone → iOS Shortcut → POST CSV → Tailscale (HTTPS) → Upload Server → daily_import.py
```

Your phone uploads CSV files over Tailscale's encrypted network directly to a small HTTP server on your computer. The server saves the file and triggers the import pipeline immediately.

This is **optional** — the standard iCloud sync + cron workflow continues to work as a fallback.

## Prerequisites

- [Tailscale](https://tailscale.com) installed on both your phone and computer (same tailnet)
- [Bun](https://bun.sh) runtime installed on your computer
- [Health Auto Export](https://apps.apple.com/us/app/health-auto-export-json-csv/id1115567069) iOS app (or any app that exports HealthKit data as CSV)
- `config.yaml` set up with your `icloud_folder` path (where CSVs are saved)

## Setup

### 1. Generate an Auth Token

Pick a random token for the upload server. This prevents unauthorized uploads.

```bash
# Generate a random token
openssl rand -hex 24
```

Save this token — you'll need it for both the server and the iOS Shortcut.

### 2. Start the Upload Server

```bash
cd /path/to/outlive-protocol
HEALTH_UPLOAD_TOKEN=your-token-here bun run server/upload-server.ts
```

The server listens on `127.0.0.1:8766` by default (localhost only — not exposed to your network).

**Environment variables:**

| Variable | Default | Description |
|----------|---------|-------------|
| `HEALTH_UPLOAD_PORT` | `8766` | Server port |
| `HEALTH_UPLOAD_TOKEN` | (required) | Bearer token for authentication |
| `HEALTH_UPLOAD_DIR` | from `config.yaml` | Override the directory where CSVs are saved |
| `HEALTH_IMPORT_SCRIPT` | `server/trigger-import.sh` | Override the import trigger script |

### 3. Expose via Tailscale Serve

Tailscale Serve creates an HTTPS endpoint on your tailnet that proxies to the local server:

```bash
tailscale serve --bg --set-path /health http://127.0.0.1:8766
```

Your upload endpoint is now available at:
```
https://your-machine.tail*.ts.net/health/upload
```

Verify it works:
```bash
curl https://your-machine.tail*.ts.net/health/status
# → {"ok":true,"service":"health-upload","uploadDir":"..."}
```

### 4. Auto-Start on Boot (macOS)

Create a LaunchAgent so the server starts automatically:

**File:** `~/Library/LaunchAgents/ai.openclaw.health-upload.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>ai.openclaw.health-upload</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/bun</string>
    <string>run</string>
    <string>/path/to/outlive-protocol/server/upload-server.ts</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HEALTH_UPLOAD_TOKEN</key>
    <string>your-token-here</string>
  </dict>
  <key>StandardOutPath</key>
  <string>/tmp/health-upload.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/health-upload.err.log</string>
</dict>
</plist>
```

Load it:
```bash
launchctl load ~/Library/LaunchAgents/ai.openclaw.health-upload.plist
```

**For Linux (systemd):**

```ini
[Unit]
Description=Health Upload Server
After=network.target

[Service]
Type=simple
Environment=HEALTH_UPLOAD_TOKEN=your-token-here
ExecStart=/path/to/bun run /path/to/outlive-protocol/server/upload-server.ts
Restart=always

[Install]
WantedBy=multi-user.target
```

### 5. iOS Shortcut

Create a Shortcut called "Health Export":

1. **Get Files** — Select the CSV files from Health Auto Export's output folder
2. **Repeat with Each** (for each file):
   - **Get Contents of URL**
     - URL: `https://your-machine.tail*.ts.net/health/upload`
     - Method: POST
     - Headers: `Authorization: Bearer your-token-here`
     - Request Body: Form
     - Add field: File, key `file`, value: Repeat Item
3. **Show Notification** — "✅ Health data uploaded"

**Tip:** If you have Health Auto Export Premium, you can trigger this Shortcut automatically after each export via Shortcuts Automation.

## Testing

```bash
# Test with a sample CSV
echo "Date/Time,Value,Unit,Source
2026-01-01 08:00:00,72,count/min,Apple Watch" > /tmp/test.csv

curl -X POST https://your-machine.tail*.ts.net/health/upload \
  -H "Authorization: Bearer your-token-here" \
  -F "file=@/tmp/test.csv"

# Expected: {"ok":true,"filename":"test.csv","size":...,"status":"importing"}
```

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `Unauthorized` | Check your Bearer token matches |
| `Connection refused` | Is the server running? `curl http://127.0.0.1:8766/status` |
| `Could not resolve host` | Is Tailscale connected? `tailscale status` |
| Upload succeeds but no import | Check `trigger-import.sh` can run: `bash server/trigger-import.sh` |
| Mac unreachable from phone | Corporate VPN may block Tailscale. Disconnect VPN first. |
| LaunchAgent won't start | Check logs: `cat /tmp/health-upload.err.log` |

## Security

- **Localhost only:** The server binds to `127.0.0.1` — it's not accessible from your local network
- **Tailscale encryption:** All traffic between devices is encrypted end-to-end via WireGuard
- **Token auth:** Every upload requires a valid Bearer token
- **File validation:** Only `.csv` files under 50MB are accepted
- **No code execution:** The server only saves files and calls the import script

## Architecture Note

This upload server is a **consumer** of the outlive-protocol import pipeline. It saves files to the same folder that `daily_import.py` scans, then triggers the import. The import pipeline itself is unchanged — it handles deduplication (via SHA-256 file hashing), file type routing, and database insertion.

If the same file arrives via both Tailscale upload and iCloud sync, `daily_import.py` will only import it once.
