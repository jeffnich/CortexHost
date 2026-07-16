# CortexHost

Setting this project up or operating it? Read `AGENTS.md` — it's the
authoritative runbook (definition of done, steps, failure modes, security
invariants). `README.md` has the human-facing overview and architecture.

Quick orientation: `services/` = the three deployables (mcp, brief, map),
`capture/` = local ingestion scripts, `docker-compose.yml` boots everything,
`./setup.sh` is the one-shot installer, `./verify.sh` probes health.
