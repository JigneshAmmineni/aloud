# Aloud — Deployment (Google Compute Engine)

How to run Aloud on a single GCE VM with a real domain and HTTPS, plus everything
you need to navigate, troubleshoot, and update the box afterward.

**Decisions baked into this doc** (change any by swapping the noted value):
- **Region/zone:** `us-west1` / `us-west1-b` (Oregon). Matches your West Coast
  location — keeps the audio round-trip short, which protects the 3s latency budget.
- **Machine:** `e2-small` (2 vCPU shared, 2 GB RAM) + a 2 GB swap file. Resize to
  `e2-medium` later when the memory layer lands (§14).
- **Domain:** you're buying one. This doc uses **`aloud.example.com`** as a
  placeholder — replace it everywhere with your real domain.
- **IP:** a *reserved static* external IP, so restarts/resizes never change it and
  your DNS record stays valid.

---

## 0. The architecture — what runs where

```
                    ┌─────────────────────── the internet ───────────────────────┐
                    │                                                             │
   your phone ──────┤  1. HTTPS (TCP 443)  ─── page, JS, signaling                │
   (browser)        │  2. WebRTC media (UDP) ─── live audio, both directions      │
                    │                                                             │
                    └──────────────────────────────┬──────────────────────────────┘
                                                   │  public IP (reserved static)
        ┌──────────────────────────── GCE VM (Debian, Docker) ──────────────────────────┐
        │                                                                                │
        │   Caddy ──┬── TCP 443/80 ── terminates HTTPS, holds the Let's Encrypt cert     │
        │           │                                                                    │
        │           ├── path /  ──────────────▶  frontend  (Next.js, :3000)              │
        │           └── path /api,/start,... ─▶  backend   (FastAPI + Pipecat, :7860)    │
        │                                              │                                 │
        │   backend ◀── UDP media (host network) ──────┘  (phone ↔ backend directly)     │
        │      │                                                                         │
        │      ├──▶ Postgres (:5432, loopback only)                                       │
        │      └──▶ Deepgram / Google / Cartesia  (outbound HTTPS to provider APIs)       │
        └────────────────────────────────────────────────────────────────────────────────┘
```

Two distinct traffic paths reach the VM, which is why the firewall opens two things:
1. **TCP 80/443** — the web app and the WebRTC *signaling* (SDP exchange). Goes through Caddy.
2. **UDP** — the actual audio packets. Goes *straight* to the backend, bypassing Caddy.
   (Recall: signaling over HTTPS sets up the call, then audio flows direct over UDP.)

**Why all containers use host networking:** WebRTC picks random ephemeral UDP ports.
Docker's normal bridge networking can't forward unpredictable ports, so the backend
must bind directly to the host's network interface (`network_mode: host`). To keep
inter-container addressing simple, all four services share host networking and talk
to each other over `localhost`. Trade-off: no Docker-level network isolation between
containers — acceptable on a single-purpose box, and Postgres is still locked to
loopback as a second layer.

---

## 1. One-time prerequisites (on your laptop)

1. **Install the gcloud CLI:** https://cloud.google.com/sdk/docs/install — then:
   ```bash
   gcloud auth login
   gcloud config set project YOUR_PROJECT_ID      # the "Aloud" GCP project
   ```
   (Find the project ID in the Cloud Console top bar, or `gcloud projects list`.)

2. **Enable the APIs** this deploy uses:
   ```bash
   gcloud services enable compute.googleapis.com iap.googleapis.com
   ```
   - `compute` = Compute Engine (the VM).
   - `iap` = Identity-Aware Proxy, used for secure SSH without opening port 22 to the world (§4).

Everything from here is copy-paste. Console (point-and-click) equivalents are noted
where useful, since you asked to understand both paths.

---

## 2. Provision the VM

### 2a. Reserve a static external IP
```bash
gcloud compute addresses create aloud-ip --region=us-west1
gcloud compute addresses describe aloud-ip --region=us-west1 --format='get(address)'
```
The second command prints the IP — **write it down**, you'll point DNS at it (§3).
*Why reserve it:* an ephemeral IP can change when the VM stops; a reserved one stays
yours, so your DNS A record never goes stale (this is the move-day problem we
discussed — reserving sidesteps it).

### 2b. Create the firewall rules
GCP firewall rules apply to VMs by **network tag**. We tag our VM `aloud` and write
three rules:

```bash
# Web traffic: HTTPS + HTTP (HTTP only so Caddy can redirect to HTTPS and pass ACME)
gcloud compute firewall-rules create aloud-web \
  --direction=INGRESS --action=ALLOW --rules=tcp:80,tcp:443 \
  --source-ranges=0.0.0.0/0 --target-tags=aloud

# WebRTC media: UDP. aiortc uses ephemeral ports we can't predict, so we open the
# UDP range. Only the media listener uses it; stray packets hit closed ports and drop.
gcloud compute firewall-rules create aloud-webrtc \
  --direction=INGRESS --action=ALLOW --rules=udp:1-65535 \
  --source-ranges=0.0.0.0/0 --target-tags=aloud

# SSH via IAP only (Google's tunnel range), NOT open to the whole internet.
gcloud compute firewall-rules create aloud-ssh-iap \
  --direction=INGRESS --action=ALLOW --rules=tcp:22 \
  --source-ranges=35.235.240.0/20 --target-tags=aloud
```

> **Decision — SSH exposure (two ways, pick one):**
> - **IAP (default above, most secure):** port 22 is reachable *only* through Google's
>   authenticated tunnel (`35.235.240.0/20`). Nothing on the open internet can even
>   see your SSH port. You connect with `--tunnel-through-iap` (§4).
> - **Your-IP-only (simpler):** if IAP gives you trouble, delete `aloud-ssh-iap` and
>   instead allow 22 from just your home IP:
>   ```bash
>   gcloud compute firewall-rules create aloud-ssh-myip \
>     --direction=INGRESS --action=ALLOW --rules=tcp:22 \
>     --source-ranges=$(curl -s ifconfig.me)/32 --target-tags=aloud
>   ```
>   Downside: your home IP changes periodically, so you'll re-run this occasionally.
>   IAP is the cleaner long-term answer.

There is **no firewall rule for 5432 (Postgres) or 7860 (backend)** — by omission,
the internet cannot reach them. That's the point.

### 2c. Create the VM
```bash
gcloud compute instances create aloud \
  --zone=us-west1-b \
  --machine-type=e2-small \
  --image-family=debian-12 --image-project=debian-cloud \
  --boot-disk-size=20GB \
  --address=aloud-ip \
  --tags=aloud
```
- `--image-family=debian-12` — a clean Debian Linux base. We install only Docker on it.
- `--address=aloud-ip` — attaches the static IP from 2a.
- `--tags=aloud` — makes the firewall rules apply to this VM.

**Console equivalent:** Compute Engine → Create Instance → set region us-west1, machine
e2-small, boot disk Debian 12 / 20 GB, Networking → Network tags `aloud`, External IP →
the reserved `aloud-ip`. The firewall rules above attach by tag automatically.

---

## 3. Buy the domain and point DNS at the VM

1. **Buy** a domain from any registrar — **Cloudflare** (at-cost, no markup) or
   **Porkbun** are good and cheap (~$10/yr). Namecheap also fine.
2. In the registrar's **DNS settings**, create one **A record**:
   ```
   Type: A     Name: aloud   (or @ for the root)     Value: <your static IP>     TTL: 300
   ```
   - `Name: aloud` → your app lives at `aloud.example.com`. `Name: @` → at the root domain.
   - **TTL 300** (5 min) keeps it nimble if you ever change the IP.
   - **Cloudflare only:** set the record to **"DNS only" (grey cloud)**, NOT "Proxied"
     (orange cloud). The proxy is HTTP-only and would break WebRTC's UDP media.
3. Verify it resolves (may take a few minutes):
   ```bash
   dig +short aloud.example.com      # should print your static IP
   ```

Once this returns your IP, Caddy will be able to obtain the HTTPS cert in §9.

---

## 4. Get onto the VM (SSH — every common way)

**Primary (recommended), gcloud + IAP tunnel:**
```bash
gcloud compute ssh aloud --zone=us-west1-b --tunnel-through-iap
```
gcloud generates and uploads your SSH key automatically the first time; the IAP flag
routes through Google's authenticated tunnel so you never need port 22 public.

**No-CLI option — browser SSH:** Cloud Console → Compute Engine → click **SSH** next to
the `aloud` instance. Opens a terminal in the browser, no local setup. Handy from any
machine.

**Plain SSH** (only if you chose the "your-IP-only" firewall option): once gcloud has
added your key, you can also `ssh USER@<static-ip>` directly.

> If IAP SSH ever errors with a permissions message, you need the *IAP-Secured Tunnel
> User* role — as project owner you already have it, but a collaborator would need
> `gcloud projects add-iam-policy-binding ... --role=roles/iap.tunnelResourceAccessor`.

---

## 5. Install Docker on the VM

SSH in (§4), then:
```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
exit                       # log out and back in so the group change takes effect
```
Reconnect (§4), then confirm:
```bash
docker --version
docker compose version
```

---

## 6. Add a swap file (so the frontend build doesn't run out of RAM)

`e2-small` has 2 GB RAM; the Next.js production build can briefly want more. A 2 GB
swap file (disk used as overflow memory) makes the build safe:
```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab    # persists across reboots
free -h                                                       # confirm: Swap shows 2.0Gi
```
(If you chose e2-medium, this step is optional — but harmless to keep.)

---

## 7. Get the code and secrets onto the VM

```bash
cd ~
git clone https://github.com/JigneshAmmineni/aloud.git
cd aloud
```
> If the repo is **private**, the clone will prompt for credentials. Easiest path:
> install the GitHub CLI on the VM (`sudo apt install gh -y`), run `gh auth login`,
> then clone. Or use a Personal Access Token as the password.

**Create the secrets file** (the VM has no `.env` yet — you create it by hand; never
commit it). Use the values you already have locally:
```bash
nano .env
```
Paste, filling in your real keys:
```
DEEPGRAM_API_KEY=...
GOOGLE_API_KEY=...
CARTESIA_API_KEY=...
CARTESIA_VOICE_ID=
LLM_MODEL=gemini-2.5-flash
STT_PROVIDER=deepgram_flux
LLM_PROVIDER=google
TTS_PROVIDER=cartesia
TTS_SANITIZE_ENABLED=true
LOG_LEVEL=INFO
POSTGRES_PASSWORD=<pick-a-strong-password>
```
Then lock its permissions so only your user can read it:
```bash
chmod 600 .env
```
*(Note `LOG_LEVEL=INFO` for prod — DEBUG logs full transcript text; INFO keeps sensitive
content out of routine logs. And set a strong `POSTGRES_PASSWORD`; the prod compose
below reads it.)*

---

## 8. Production config files

These three files differ from the local dev setup (no hot-reload, real frontend build,
Caddy added, Postgres locked down). Create them on the VM inside `~/aloud`.

### 8a. `docker-compose.prod.yml`
```bash
nano docker-compose.prod.yml
```
```yaml
services:
  caddy:
    image: caddy:2-alpine
    network_mode: host          # binds 80/443 on the VM; serves HTTPS, routes by path
    restart: unless-stopped
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile
      - caddy_data:/data        # persists the issued cert across restarts
      - caddy_config:/config

  frontend:
    build: ./frontend
    network_mode: host          # serves on localhost:3000 (not firewalled → Caddy-only)
    restart: unless-stopped
    depends_on: [backend]

  backend:
    build: ./backend
    network_mode: host          # REQUIRED: WebRTC UDP media binds directly to the host
    restart: unless-stopped
    env_file: .env
    environment:
      - DATABASE_URL=postgresql+asyncpg://aloud:${POSTGRES_PASSWORD}@127.0.0.1:5432/aloud
    depends_on: [db]

  db:
    image: postgres:16-alpine
    network_mode: host
    restart: unless-stopped
    command: postgres -c listen_addresses=127.0.0.1   # loopback only: not on any public iface
    environment:
      - POSTGRES_USER=aloud
      - POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
      - POSTGRES_DB=aloud
    volumes:
      - pgdata:/var/lib/postgresql/data

volumes:
  caddy_data:
  caddy_config:
  pgdata:
```
Postgres is protected twice here: no firewall rule for 5432 **and** `listen_addresses=
127.0.0.1` so it only answers on the loopback interface.

### 8b. `frontend/Dockerfile` (production build)
The dev setup ran `npm run dev` live; prod needs a real compiled build. Create:
```bash
nano frontend/Dockerfile
```
```dockerfile
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM node:22-alpine
WORKDIR /app
ENV NODE_ENV=production       # this is what strips the dev-only window.__aloudClient hook
COPY --from=build /app ./
EXPOSE 3000
CMD ["npm", "start"]          # next start, serves the compiled app on :3000
```

### 8c. `Caddyfile`
```bash
nano Caddyfile
```
```
{
    email you@example.com      # for Let's Encrypt expiry notices (optional but nice)
}

aloud.example.com {
    # signaling + health go to the backend; everything else to the frontend
    @backend path /api/* /start /sessions/* /healthz
    handle @backend {
        reverse_proxy localhost:7860
    }
    handle {
        reverse_proxy localhost:3000
    }
}
```
Replace `aloud.example.com` with your domain and `you@example.com` with your email.
Caddy reads the domain, automatically obtains and renews the Let's Encrypt cert, and
routes each request to the right container.

### 8d. The STUN / public-IP config — nothing to change
Already handled in code: the backend creates its WebRTC connection with
`stun:stun.l.google.com:19302`. On GCE, the VM's NIC only sees a private IP, but STUN
makes the backend discover its **public** IP and advertise that to the phone. So the
phone connects to the public IP, GCP's 1:1 NAT forwards it in, and audio flows. No edit
needed — but §11 shows how to verify it's working and what to do if it isn't.

---

## 9. Launch

From `~/aloud` on the VM:
```bash
docker compose -f docker-compose.prod.yml up -d --build
```
First run builds the images (a few minutes) and starts all four containers. Watch Caddy
obtain the cert:
```bash
docker compose -f docker-compose.prod.yml logs -f caddy
```
You want a line like `certificate obtained successfully` for your domain. (If it loops
on errors, jump to §15.)

---

## 10. Verification checklist

Run top-to-bottom; each rung depends on the last:

1. **DNS:** `dig +short aloud.example.com` → your static IP.
2. **Cert + HTTPS:** `curl -I https://aloud.example.com` → `HTTP/2 200`, no cert warning.
3. **Backend health:** `curl https://aloud.example.com/healthz` → `{"status":"ok"}`.
4. **Page loads:** open `https://aloud.example.com` in a desktop browser → the Aloud UI.
5. **Mic + conversation:** tap Talk, allow mic, have a short conversation. (Desktop first —
   isolates app issues from phone/cellular issues.)
6. **The real test — phone on cellular:** open the URL on your phone with **Wi-Fi off**
   (forces the CGNAT path). Tap Talk, talk. This proves the public-IP/UDP path end to end.
7. **Artifacts:** ask "write that up for me" → the panel appears.
8. **Latency sanity:** `docker compose -f docker-compose.prod.yml logs backend | grep turn.latency`
   → end-to-end under the 3s budget (expect a bit higher than localhost due to the
   network leg — watch that us-west1 distance if you demoed from the East Coast).

---

## 11. WebRTC media — why it works, and how to debug it

This is the part most likely to need attention on first deploy, so here's the mental
model and the tools.

**The happy path:** phone loads page (HTTPS via Caddy) → POSTs SDP offer to `/api/offer`
(Caddy → backend) → backend gathers ICE candidates, including a *server-reflexive* (srflx)
candidate carrying the VM's **public IP** (discovered via STUN) → both sides hole-punch →
audio flows over UDP directly to the backend (host network) → firewall's open UDP range
lets it in.

**If audio doesn't flow** (page loads, mic permission granted, but no agent voice / no
response), check in order:

1. **UDP firewall:** `gcloud compute firewall-rules list` → confirm `aloud-webrtc`
   exists and allows `udp:1-65535` for tag `aloud`.
2. **Candidates:** on desktop Chrome open `chrome://webrtc-internals` *before* connecting,
   start a session, and look at the ICE candidates. You should see an `srflx` candidate
   with your **public IP**. If you only see the private `10.x`/`172.x` host candidate,
   STUN isn't reaching out — check outbound UDP isn't blocked (GCE allows egress by
   default, so this is rare).
3. **Selected pair:** in webrtc-internals, the connection should reach `state: connected`
   /`completed` with a selected candidate pair. Stuck in `checking` = no path found.
4. **Backend logs:** `docker compose -f docker-compose.prod.yml logs backend | grep -i ice`
   for connection-state transitions.

**If it still fails on some networks** (strict corporate/carrier NAT that blocks UDP
entirely): that's the deferred **TURN** case. Add a TURN server to the backend's ICE
config (rented Twilio/Cloudflare/Metered, or self-hosted coturn) — one `IceServer(...)`
entry. Not needed for the common case; only if logs show real-world connection failures.

---

## 12. SSH & common commands — your navigation/troubleshooting toolkit

### Getting in
```bash
gcloud compute ssh aloud --zone=us-west1-b --tunnel-through-iap   # primary
# or: Console → Compute Engine → SSH button (browser, no setup)
```

### Where things live
```bash
cd ~/aloud                      # the repo / all config + compose files
cat .env                        # your secrets (don't share output)
```

### Containers & the app (all use the -f prod file)
```bash
cd ~/aloud
docker compose -f docker-compose.prod.yml ps          # what's running + status
docker compose -f docker-compose.prod.yml logs -f backend     # live backend logs
docker compose -f docker-compose.prod.yml logs --tail 100 caddy
docker compose -f docker-compose.prod.yml restart backend     # restart one service
docker compose -f docker-compose.prod.yml down                # stop everything
docker compose -f docker-compose.prod.yml up -d               # start everything
docker compose -f docker-compose.prod.yml up -d --build       # rebuild + start (after code changes)
```
Tip: set `alias dc='docker compose -f docker-compose.prod.yml'` in `~/.bashrc` so you
can type `dc ps`, `dc logs -f backend`, etc.

### Logs & traffic

**The richest view — backend structured logs (JSON).** Sessions, per-turn latency, the
WebRTC/UDP connection setup, transcripts, tool calls. Best workflow: follow this in one
window, then have a conversation on the site — you watch it all happen live.
```bash
docker compose -f docker-compose.prod.yml logs -f backend
```
Filter for specific events (the logs are JSON, so `grep` works well):
```bash
dc logs backend | grep -iE "ice|candidate|connection state"  # WebRTC/UDP path coming up:
                                                              # candidate gathering, the srflx
                                                              # (public-IP) candidate, and state
                                                              # → connected/completed = media is up
dc logs backend | grep turn.latency        # per-turn end-of-speech → first-audio (ERROR if >3s)
dc logs backend | grep -E "session|transport"   # session lifecycle (connect/start/end)
dc logs backend | grep transcript          # transcript rows being written
dc logs backend | grep tool.invoked        # artifact / tool calls
```

**HTTP / HTTPS requests — Caddy.** Page loads and signaling POSTs (`/api/offer`, `/start`).
```bash
docker compose -f docker-compose.prod.yml logs -f caddy
```
Note: by default Caddy logs errors + lifecycle, **not a line per request**. For a clean
access log (one line per HTTP request), add a `log` block to the `Caddyfile`:
```
{$ALOUD_DOMAIN} {
    log                              # access log → stdout (visible in `logs caddy`)
    # ...existing handle blocks...
}
```
then `dc up -d` to apply.

**Raw UDP at the socket/packet level** (rarely needed — the ICE state in the backend logs
usually answers "is audio flowing?"):
```bash
sudo ss -unap                                   # active UDP sockets (media ports show here)
sudo tcpdump -i any -n udp and not port 53      # live UDP packets; Ctrl+C to stop
```

When this stack later ships its logs to **GCP Cloud Logging** (Cloud Run/GKE, or the
logging agent on this VM), the same backend JSON becomes searchable in the console —
`jsonPayload.session_id`, `jsonPayload.event`, etc. — no code change. For now, `grep` on
the container logs is the fastest view.

### The database
```bash
# open a SQL shell (container name from `docker compose ... ps`):
docker compose -f docker-compose.prod.yml exec db psql -U aloud -d aloud
#   inside psql:  \dt   (list tables)   SELECT * FROM sessions ORDER BY started_at DESC LIMIT 5;   \q (quit)

# one-off query without entering the shell:
docker compose -f docker-compose.prod.yml exec db \
  psql -U aloud -d aloud -c "SELECT role, left(text,60) FROM transcript_events ORDER BY id DESC LIMIT 10;"
```
To use a GUI DB tool (TablePlus/pgAdmin) from your laptop **without** opening 5432:
port-forward over SSH (temporary, your machine only) —
```bash
gcloud compute ssh aloud --zone=us-west1-b --tunnel-through-iap -- -L 5432:localhost:5432
# now connect your GUI to localhost:5432; the tunnel closes when you end the SSH session
```

### Health, resources, system
```bash
free -h                         # RAM + swap usage
df -h                           # disk usage
top   (or: htop after `sudo apt install htop`)   # live CPU/mem per process
docker stats                    # live per-container CPU/mem
sudo journalctl -u docker -n 50 # docker daemon logs
```

### Exiting
```bash
exit        # leaves the SSH session; containers keep running (restart: unless-stopped)
```

---

## 13. Updating the app after a code change

Push your change to GitHub from your laptop as usual, then on the VM:
```bash
cd ~/aloud
git pull
docker compose -f docker-compose.prod.yml up -d --build
```
Only changed images rebuild. Postgres data and the Caddy cert persist (they're in named
volumes). Zero DNS or cert work needed.

---

## 14. Resizing the VM later (e2-small → e2-medium)

When the cross-session memory layer needs more RAM:
```bash
gcloud compute instances stop aloud --zone=us-west1-b
gcloud compute instances set-machine-type aloud --zone=us-west1-b --machine-type=e2-medium
gcloud compute instances start aloud --zone=us-west1-b
```
The reserved IP, boot disk, and all data persist; only the machine size changes
(~2 min downtime). Containers come back automatically (`restart: unless-stopped`); if not,
SSH in and `docker compose -f docker-compose.prod.yml up -d`. To go back, set the type to
`e2-small` the same way.

---

## 15. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Caddy log loops on cert errors | DNS not pointing at the VM yet, or :80 blocked | `dig +short yourdomain` must show the static IP; confirm `aloud-web` firewall rule allows tcp:80; wait for DNS TTL, then `docker compose -f docker-compose.prod.yml restart caddy` |
| `https://` shows cert warning | cert not issued yet | check `logs -f caddy`; usually resolves once DNS propagates |
| Page loads, but no agent voice | UDP media not reaching backend | §11 — verify `aloud-webrtc` UDP rule, check `chrome://webrtc-internals` for an srflx candidate |
| "Missing required environment variables" in backend log | `.env` missing/incomplete | `cat .env`, ensure the three keys are set; `chmod 600 .env`; rebuild |
| Backend can't reach DB | wrong password / db not up | confirm `POSTGRES_PASSWORD` matches in `.env`; `docker compose ... ps` shows db healthy |
| Frontend build killed / OOM | swap not enabled on e2-small | redo §6 (`free -h` should show 2 GB swap), rebuild |
| Works on Wi-Fi, fails on cellular | strict carrier NAT blocking UDP | the deferred TURN case (§11) |
| Can't SSH in | IAP firewall/role | confirm `aloud-ssh-iap` rule exists; use `--tunnel-through-iap`; or temporarily add the your-IP rule (§2b) |

---

## 16. Cost (rough, USD/month)

| Item | Cost |
|---|---|
| e2-small VM (24/7) | ~$13 |
| Reserved static IP (while attached to a running VM) | ~$0 (free in use; ~$3/mo only if idle/unattached) |
| 20 GB boot disk | ~$0.80 |
| Network egress (demo traffic) | a few cents |
| Domain | ~$10 / **year** |
| **Total** | **~$14–15/mo + $10/yr** |

Provider API usage (Deepgram/Gemini/Cartesia) bills separately per use, as today.
To pause spend between demos: `gcloud compute instances stop aloud --zone=us-west1-b`
(you stop paying for compute; keep the static IP attached or it may be reclaimed).

---

## 17. Security checklist (what this setup already does, and the gaps)

**Handled:**
- SSH not public — IAP tunnel only (or your-IP-only if you chose that).
- Postgres not public — no firewall rule + bound to loopback.
- Backend port 7860 not public — no firewall rule (only Caddy reaches it locally).
- HTTPS enforced, auto-renewing cert.
- Secrets in `.env`, `chmod 600`, never committed (gitignored).
- The dev-only `window.__aloudClient` hook is stripped by the production build.
- `LOG_LEVEL=INFO` keeps transcript text out of routine logs.

**Known gaps (fine for a demo, address before real users):**
- **No app auth** — anyone with the URL can use it and spend your API credits. Cheap
  interim guard: add HTTP Basic Auth in the Caddyfile (`basicauth` directive), or keep
  the domain unshared. Real fix: the auth layer in PLAN.md.
- **Secrets in a plain file** — fine for single-user; GCP Secret Manager is the upgrade.
- **No DB backups** — `pgdata` lives on the VM disk. Before you have data worth keeping,
  add a periodic `pg_dump` to a GCS bucket, or migrate to Cloud SQL (managed backups).
- **OS patching** — enable automatic security updates: `sudo apt install unattended-upgrades`.
```
