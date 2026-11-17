#!/usr/bin/env bash
# DevServer — validate the compose graph without a Docker daemon.
#
#   bash scripts/compose-check.sh
#
# `docker compose config` is a CLIENT-side operation: it interpolates, merges
# and validates the files without ever talking to the engine. So this runs on a
# machine with Docker Desktop stopped, or not installed at all beyond the CLI
# plugin — which is exactly when a compose typo would otherwise go unnoticed
# until a deploy.
#
# Three classes of check:
#
#   1. PARSE — every legal `-f` combination merges and validates.
#
#   2. DRIFT — docker-compose.yml (the dev stack, needs the repo) and
#      docker-compose.release.yml (a standalone download) describe the same
#      two-tier graph twice, on purpose. This asserts the pair still agrees on
#      every environment KEY per service and on the image reference. That check
#      exists because they HAD drifted: the dev file
#      was missing `:-` defaults on the Telegram vars (a warning on every
#      compose invocation) and published the unauthenticated worker port on all
#      interfaces where release bound it to loopback. So it also asserts that,
#      with WORKER_BIND unset, every port the worker publishes in either file
#      is bound to 127.0.0.1.
#
#   3. IMAGE — every service that runs DevServer code resolves to a tag from
#      ${DEVSERVER_IMAGE}, on its own tier's tag prefix, so neither tier can be
#      pointed at the other's image.
#
# Exit status is 0 only if all three pass. Safe to wire into CI or a pre-push
# hook; it needs python3 (for the JSON walk) and the compose CLI plugin.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../docker"

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; DIM=$'\033[2m'; OFF=$'\033[0m'
fail=0
note() { printf '%s\n' "$1"; }
ok()   { printf '%s  ok%s   %s\n' "$GREEN" "$OFF" "$1"; }
bad()  { printf '%s  FAIL%s %s\n' "$RED" "$OFF" "$1"; fail=1; }

command -v docker >/dev/null 2>&1 || { echo "docker CLI not found on PATH"; exit 2; }
docker compose version >/dev/null 2>&1 || { echo "the 'docker compose' CLI plugin is not installed"; exit 2; }

# Resolve a python by RUNNING it, never with `command -v`: on Windows the
# python3 App Execution Alias stub is on PATH, so `command -v python3` succeeds
# while the interpreter only prints "Python was not found" and exits non-zero —
# which silently turned the drift check below into a guaranteed failure.
PY=""
for cand in python3 python "py -3"; do
  if $cand -c '' >/dev/null 2>&1; then PY="$cand"; break; fi
done
[ -n "$PY" ] || { echo "no working python found (tried python3, python, py -3) — needed for the drift check"; exit 2; }

# Interpolation defaults. Every var these files read has a `:-` fallback, so the
# only reason to set anything here is to keep the output free of "variable is
# not set" warnings — and a var WITHOUT a fallback will still warn, which is
# itself a finding.
export PGPASSWORD="${PGPASSWORD:-checkonly}"
# The loopback assertion below checks the DEFAULT binding, so an operator's
# deliberate WORKER_BIND=0.0.0.0 in this shell must not mask (or fail) it.
unset WORKER_BIND

# ── 1. Parse every legal combination ────────────────────────────────────────
# Keep in lockstep with scripts/_lib.sh::docker_compose_files and the header
# comments in each overlay. host-db and local are overrides of the base;
# host-worker and release are self-contained; hardened is an overlay of the base.
#
# ORDER MATTERS in the last combination, and this check is how we found out.
# host-db.yml does `postgres: !reset null` while hardened.yml re-declares a
# `postgres:` block carrying only deploy/logging tweaks. Merged in the order the
# hardened overlay's own header used to document (host-db, then hardened), the
# re-declaration resurrects a service with no image and compose rejects the
# whole project. host-db must come LAST so its removal wins.
COMBOS=(
  "docker-compose.yml"
  "docker-compose.yml docker-compose.host-db.yml"
  "docker-compose.yml docker-compose.local.yml"
  "docker-compose.yml docker-compose.hardened.yml"
  "docker-compose.yml docker-compose.hardened.yml docker-compose.host-db.yml"
  "docker-compose.host-worker.yml"
  "docker-compose.release.yml"
)

note "${DIM}── parse ──────────────────────────────────────────────────${OFF}"
for combo in "${COMBOS[@]}"; do
  args=()
  for f in $combo; do
    # A file missing from this checkout is reported as a skip, not a failure.
    if [ ! -f "$f" ]; then
      printf '%s  skip%s %s %s(%s absent)%s\n' "$YELLOW" "$OFF" "$combo" "$DIM" "$f" "$OFF"
      continue 2
    fi
    args+=(-f "$f")
  done
  if err="$(docker compose "${args[@]}" --profile bundled-db config -q 2>&1)"; then
    if [ -n "$err" ]; then
      # A clean parse that still emits warnings is the "variable is not set"
      # class of finding — report it, but do not fail the run.
      printf '%s  warn%s %s\n' "$YELLOW" "$OFF" "$combo"
      printf '%s\n' "$err" | sed 's/^/        /'
    else
      ok "$combo"
    fi
  else
    bad "$combo"
    printf '%s\n' "$err" | sed 's/^/        /'
  fi
done

# ── 2 + 3. Drift between the dev and release graphs, and image provenance ───
note ""
note "${DIM}── drift: docker-compose.yml vs docker-compose.release.yml ─${OFF}"

dev_json="$(docker compose -f docker-compose.yml --profile bundled-db config --format json 2>/dev/null)"
rel_json="$(docker compose -f docker-compose.release.yml --profile bundled-db config --format json 2>/dev/null)"

if ! $PY - "$dev_json" "$rel_json" <<'PYEOF'
import json, sys

dev, rel = (json.loads(a).get("services") or {} for a in sys.argv[1:3])
problems = []

# The two tiers that run DevServer code. postgres is upstream pgvector in both
# and is compared only on its own terms below.
for name in ("worker", "web"):
    d, r = dev.get(name), rel.get(name)
    if not d or not r:
        problems.append(f"{name}: missing from " + ("docker-compose.yml" if not d else "docker-compose.release.yml"))
        continue

    dk = set((d.get("environment") or {}))
    rk = set((r.get("environment") or {}))
    # DEPLOY_MODE etc. must match exactly; anything present on one side only is
    # drift, whichever side it is on.
    only_dev, only_rel = sorted(dk - rk), sorted(rk - dk)
    if only_dev:
        problems.append(f"{name}: env keys only in docker-compose.yml: {', '.join(only_dev)}")
    if only_rel:
        problems.append(f"{name}: env keys only in docker-compose.release.yml: {', '.join(only_rel)}")

    if d.get("command") != r.get("command"):
        problems.append(f"{name}: command differs — {d.get('command')} vs {r.get('command')}")

# The worker has no general authentication: by default every port it publishes
# must be loopback-only (WORKER_BIND is unset above, so this is the default).
for label, graph in (("docker-compose.yml", dev), ("docker-compose.release.yml", rel)):
    for port in (graph.get("worker") or {}).get("ports") or []:
        host_ip = port.get("host_ip") if isinstance(port, dict) else None
        if host_ip != "127.0.0.1":
            problems.append(f"worker [{label}]: port {port!r} is published on {host_ip or 'all interfaces'}, not 127.0.0.1")

# Image provenance: every DevServer tier resolves to the one repository, on the
# tag prefix that belongs to its tier. Each target's CMD and HEALTHCHECK are
# baked into the image, so pointing a tier at the other tier's tag is the one
# way left to get a container that starts the wrong thing — and it would look
# perfectly healthy, because the image it actually ran would probe itself.
for label, graph in (("docker-compose.yml", dev), ("docker-compose.release.yml", rel)):
    for name, prefix in (("worker", "worker-"), ("web", "web-")):
        svc = graph.get(name) or {}
        img = svc.get("image") or ""
        if "devserver" not in img:
            problems.append(f"{name} [{label}]: image {img!r} is not a DevServer image")
        elif prefix not in img.rsplit(":", 1)[-1]:
            problems.append(f"{name} [{label}]: image {img!r} is not on the {prefix!r} tag prefix")

if problems:
    for p in problems:
        print("        " + p)
    sys.exit(1)
PYEOF
then
  bad "the dev and release graphs have drifted"
else
  ok "env keys, worker loopback binding and per-tier image provenance agree"
fi

note ""
if [ "$fail" = 0 ]; then
  printf '%sAll compose checks passed.%s\n' "$GREEN" "$OFF"
else
  printf '%sCompose checks FAILED.%s\n' "$RED" "$OFF"
fi
exit "$fail"
