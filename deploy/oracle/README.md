# Oracle Cloud Deployment

This bundle deploys the existing Neon-backed FastAPI service and Telegram bot to one Oracle Compute instance. Docker Compose keeps both application ports private; Caddy is the only public service and provides HTTPS on ports 80 and 443.

## Architecture

```text
GitHub Pages / Telegram
          |
       HTTPS 443
          |
    Oracle public IP
          |
        Caddy
       /     \
  neon-api  telegram-bot
       |          |
       +---- Neon PostgreSQL
```

## 1. Oracle networking

In the existing `market-intelligence-vcn`:

1. Use or create a public subnet with public IPv4 assignment enabled.
2. Attach an Internet Gateway to the VCN.
3. Ensure the subnet route table has `0.0.0.0/0` routed to that Internet Gateway.
4. Create a Network Security Group for the instance.
5. Allow TCP 22 only from your current public IP using a `/32` CIDR.
6. Allow TCP 80 and TCP 443 from `0.0.0.0/0`.
7. Optionally allow UDP 443 from `0.0.0.0/0` for HTTP/3.
8. Keep application ports such as 8000 closed; they are internal Docker ports.

## 2. Oracle instance

Recommended starting configuration:

- Image: Ubuntu 24.04 LTS
- Shape: `VM.Standard.A1.Flex`, 2 OCPUs and 12 GB RAM where capacity and account limits permit
- Boot volume: 50 GB
- VCN: `market-intelligence-vcn`
- Subnet: the public subnet above
- Public IPv4: enabled; reserve it after creation so DNS does not change
- SSH authorized key: contents of `C:\Users\User\OneDrive\Coding\DB_builder_env\ssh\oracle\OCI-ssh-key-2026-08-23.key.pub`
- SSH user after launch: `ubuntu`

The E2 micro shape is generally too memory-constrained for two Python services plus pandas, SQLAlchemy, Docker and a reverse proxy.

## 3. DNS and HTTPS

Create two DNS records pointing to the reserved Oracle public IP:

- `api.138.2.69.165.sslip.io`
- `bot.138.2.69.165.sslip.io`

Caddy obtains and renews public TLS certificates automatically after both names resolve and Oracle ports 80/443 are open. If you do not own a domain, temporary `sslip.io` names can be used, such as `api.<public-ip>.sslip.io` and `bot.<public-ip>.sslip.io`, but an owned domain is preferable for production stability.

## 4. Local SSH key

The verified key pair is stored outside every repository:

```text
C:\Users\User\OneDrive\Coding\DB_builder_env\ssh\oracle\OCI-ssh-key-2026-08-23.key
C:\Users\User\OneDrive\Coding\DB_builder_env\ssh\oracle\OCI-ssh-key-2026-08-23.key.pub
```

Never upload the private key to Oracle Console, GitHub, the VM or a repository. Oracle Console receives only the `.pub` key when the instance is created.

`DB_builder_env` is outside the agent project and Git repositories, but it is still under OneDrive. The private key may therefore be synchronized to your Microsoft account. Keep OneDrive account protection and disk encryption enabled, or move only the private key to a non-synchronized local secret directory if cloud synchronization is not acceptable.

Connect with:

```powershell
ssh -i "C:\Users\User\OneDrive\Coding\DB_builder_env\ssh\oracle\OCI-ssh-key-2026-08-23.key" ubuntu@<ORACLE_PUBLIC_IP>
```

## 5. Environment files

Keep local values in the existing external directory. Do not create `.env` files in either repository.

### `C:\Users\User\OneDrive\Coding\DB_builder_env\neon-api\.env`

```dotenv
NEON_DB_USER=
NEON_DB_PASSWORD=
NEON_DB_HOST=
NEON_DB_NAME=
NEON_DB_PORT=5432
NEON_DB_SSLMODE=require
NEON_DB_CHANNEL_BINDING=require
MARKET_API_BASE_URL=https://api.138.2.69.165.sslip.io
PUBLIC_API_URL=
```

These separate fields are enough; the application constructs the SQLAlchemy connection safely and passwords do not need manual URL encoding. `NEON_DB_HOST` should be the pooled host when Neon provides one. Alternatively, set only `NEON_DATABASE_URL` to a complete pooled URL. Prefer a dedicated read-only API role rather than `neondb_owner`. The application retains `neon_password` only as a legacy local fallback.

### `C:\Users\User\OneDrive\Coding\DB_builder_env\market-intelligence-telegram-bot\.env`

```dotenv
TG_TOKEN=
TG_CHAT_ID=
TG_USERNAME=Ai_market_monitor_bot
TG_WEBHOOK_SECRET=
TG_RENDER_BASE_URL=https://bot.138.2.69.165.sslip.io
TG_REPORT_URL=https://wkyjim.github.io/market-dashboard/data/latest-report.md
TG_DASHBOARD_URL=https://wkyjim.github.io/market-dashboard/#overview
MARKET_API_BASE_URL=https://api.138.2.69.165.sslip.io
REQUEST_TIMEOUT_SECONDS=30
LOG_LEVEL=INFO
REPORT_HASH_STATE_PATH=/var/lib/market-intelligence-bot/report-hash.json
```

Generate `TG_WEBHOOK_SECRET` as a long random value. The same value protects both the path and `X-Telegram-Bot-Api-Secret-Token` header. Do not print or commit it.

### VM-only deployment file

`/opt/market-intelligence/deploy/oracle.env` contains no credentials. It stores the two public host names and server paths. `Deploy-OracleStack.ps1` creates and uploads it.

The two secret files are uploaded to:

```text
/opt/market-intelligence/secrets/neon-api.env
/opt/market-intelligence/secrets/telegram-bot.env
```

They are mode `600` inside a mode `700` directory.

## 6. Bootstrap and deploy

After the Oracle instance is running:

```powershell
cd C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder\deploy\oracle

.\Initialize-OracleVm.ps1 -PublicIp <ORACLE_PUBLIC_IP>
```

Disconnect and reconnect once, then deploy:

```powershell
.\Deploy-OracleStack.ps1 `
  -PublicIp <ORACLE_PUBLIC_IP> `
  -ApiHost api.138.2.69.165.sslip.io `
  -BotHost bot.138.2.69.165.sslip.io
```

The deployment helper copies only the required source and deployment files. It uploads the two external environment files without displaying them.

## 7. Verify

```powershell
Invoke-RestMethod https://api.138.2.69.165.sslip.io/
Invoke-RestMethod https://bot.138.2.69.165.sslip.io/health
```

On the VM:

```bash
/opt/market-intelligence/deploy/status.sh
```

Register the Telegram webhook after the bot health endpoint passes:

```powershell
$secret = Read-Host "TG_WEBHOOK_SECRET"
$headers = @{ "X-Telegram-Bot-Api-Secret-Token" = $secret }
Invoke-RestMethod -Method Post -Uri "https://bot.138.2.69.165.sslip.io/telegram/set-webhook/$secret" -Headers $headers
```

Do not put the secret directly into shell history or documentation.

## 8. Updates

Re-run `Deploy-OracleStack.ps1` after tested source changes. It rebuilds the images and replaces containers while preserving Caddy certificates and Telegram report-hash state in Docker volumes.

Rollback is source based: restore the desired tested application files locally and rerun the same deployment command. Database schema and data are not changed by this stack.

## 9. Security notes

- Do not open PostgreSQL or port 8000 in OCI security rules.
- Use a Neon pooled URL with TLS enabled.
- Use a dedicated least-privilege Neon role for this read-only API.
- Restrict SSH ingress to your current public IP.
- Keep the Oracle private key only under `C:\Users\User\OneDrive\Coding\DB_builder_env\ssh\oracle`.
- Caddy and Uvicorn access logs are disabled for protected path-secret routes; application logs remain available through `status.sh`.
- Docker logs rotate at 10 MB with five retained files per service.
