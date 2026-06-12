# Aloud — Scrap Notes / Future Directions

Informal notes for where this project goes after the MVP demo. Not a tracked plan.
(Deferred features are formally listed in REQUIREMENTS.md §6 Out of Scope.)

---

## Deploying off localhost (when the time comes)

Cellular isn't the hard case people think — almost no production app does true
peer-to-peer. They all do **client ↔ public media server**, and CGNAT only breaks
*inbound* connections. A phone on CGNAT can always dial **out** over UDP to a server
with a public IP. Our setup (SmallWebRTC = server-side peer) is already this shape.
The problem was never cellular per se — it's that the dev server sits behind
Docker/PaaS plumbing with no public UDP.

The standard playbook, in order of how common it is:

1. **Public media server + TURN fallback.** Host the WebRTC endpoint anywhere with
   a public IP and open UDP port range (any $5 VPS — Hetzner, DigitalOcean, EC2; no
   custom engineering, just a normal box). Add a TURN server (self-hosted coturn, or
   rented: Twilio NTS, Cloudflare, Metered, Xirsys) for the ~5–15% of clients on
   networks that block UDP entirely — TURN over TCP/TLS on port 443 gets through
   almost anything, because it looks like HTTPS. This is what Zoom/Meet/Discord-class
   apps effectively do (with SFUs as the public servers).

2. **Managed WebRTC infrastructure.** Daily, LiveKit Cloud, Cloudflare Realtime,
   Agora — they run the public media edge and TURN for you. For this stack
   specifically there's also **Pipecat Cloud** (Daily's hosting built for exactly
   these bots) — swap SmallWebRTC for the Daily transport, one factory change under
   the C-2 provider abstraction.

3. **Don't use WebRTC for the media leg — WebSocket audio over TCP/443.** Works on
   any PaaS, through any firewall, no ICE at all. This is how telephony providers
   (Twilio media streams) and OpenAI's Realtime WebSocket mode work. The cost: TCP
   head-of-line blocking — a lost packet stalls the stream, so latency spikes on
   lossy cellular links instead of degrading gracefully. Pipecat ships a WebSocket
   transport, so this is a real fallback option, not a rewrite.

4. **Kubernetes/host-networking self-hosting** (how self-hosted LiveKit deploys) —
   only relevant at scale.

**Realistic path for Aloud post-demo:** a cheap VPS with public UDP + rented TURN
(~$10/mo total), or Pipecat Cloud to avoid running infrastructure. Both leave the
application code untouched.
