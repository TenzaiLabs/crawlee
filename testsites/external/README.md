# External crawler targets

This package runs three intentionally vulnerable or crawler-focused targets on one dedicated Docker bridge. Only loopback ports are published; do not expose them to an untrusted network.

## Immutable targets

| Target | Loopback URL | Pin |
| --- | --- | --- |
| ZAP CrawlGround | `http://127.0.0.1:13456/` | source commit `9a9fea6237d3b3c72788cd8e1d158ebcdb0f7a2e`, archive SHA-256 checked during build |
| OWASP Juice Shop | `http://127.0.0.1:13000/` | `v20.1.1` plus OCI manifest digest |
| ParaBank | `http://127.0.0.1:18080/parabank/` | OCI manifest digest (audited `baseline` channel) |

`pins.json` is the machine-readable pin record. The image references in `compose.yml` include digests, and CrawlGround's Dockerfile verifies its immutable source archive and pins the Node base image by digest. `manifests/` contains the integration contract for each target and freezes CrawlGround's 59-control inventory.

## Commands

Run from this directory:

```sh
./scripts/up.sh                 # bounded build/start/readiness
./scripts/health.sh             # all targets, or pass service names
./scripts/reset-crawlground.sh  # POST /reset and verify scores are empty
./scripts/reset-juice-shop.sh   # discard writable layer and recreate
./scripts/reset-parabank.sh     # recreate, then POST initializeDB
./scripts/down.sh               # retain CrawlGround score volume
./scripts/down.sh -v            # teardown and remove score volume
python3 smoke_test.py           # start, verify APIs/resets, leave running
python3 smoke_test.py --skip-start
```

Startup and reset waits default to 120 seconds. Override only the bounded wait with `EXTERNAL_TARGET_TIMEOUT_SECONDS`; no script waits indefinitely.

### CrawlGround scoring API

```sh
curl --data 'confirm=RESET' http://127.0.0.1:13456/reset
curl -X POST http://127.0.0.1:13456/set-tool \
  -H 'Content-Type: application/json' -d '{"name":"katana-baseline"}'
curl http://127.0.0.1:13456/results.json
```

Stable tool names for integration are `katana-baseline`, `browser-deterministic`, and `browser-live-model`.

### Authentication references

No plaintext credentials are stored. ParaBank's manifest refers to `PARABANK_USERNAME` and `PARABANK_PASSWORD`; its pinned reset initializes the documented `john`/`demo` seed account. Export those values only in the process running the harness. Juice Shop's baseline seed is public and its reset is a clean container recreation.

## Reset guarantees

- CrawlGround's native reset wipes every tool score and the reset script confirms the resulting report has no scored tools.
- Juice Shop and ParaBank do not receive host data mounts. Their reset scripts remove the container and create a new writable layer from the pinned image, independent of prior users, sessions, or mutable data.
- ParaBank additionally invokes its official `POST /parabank/services/bank/initializeDB` operation and the smoke test confirms the seeded account is available.

The smoke test deliberately scores one CrawlGround marker, checks `/set-tool` and `/results.json`, resets it, recreates both application targets, and rechecks readiness.
