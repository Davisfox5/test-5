# Redis setup

Redis is required for:

* Celery broker + result backend (every async task — analysis pipeline,
  email ingest, action-plan matcher, regen scheduler).
* SSE pub/sub for the notifications channel (the `action_plan.*` events
  the canvas listens for, plus chat + notification fan-out).
* Per-tenant rate limiting.

Without Redis, the API still serves, but workers crash on startup and
the SSE stream silently no-ops (best-effort by design).

## Local development

```bash
docker compose up -d redis
# Add to .env:
#   REDIS_URL=redis://localhost:6379/0
```

The data volume persists between runs. Reset with
`docker compose down -v` if you need a clean slate.

## Staging (Fly + Upstash)

Staging is configured to use Upstash Redis (`fly.toml:5` documents
this). If it isn't already provisioned, do it once:

```bash
# Auth first if you haven't.
fly auth login

# Create an Upstash Redis attached to the Fly org. Pick the
# region closest to linda-staging's primary (iad). Free plan is
# 256MB / 500K commands per day — plenty for staging.
fly redis create --org <your-org> --region iad --name linda-redis-staging --plan free

# The above prints the connection URL. Set it as a secret on
# linda-staging:
fly secrets set REDIS_URL='redis://default:<password>@<host>:<port>' --app linda-staging

# Verify it's set (won't show the value):
fly secrets list --app linda-staging | grep REDIS_URL
```

After setting the secret, Fly automatically restarts the app machines
(api + worker + beat). Watch the deploy:

```bash
fly logs --app linda-staging
```

You should see the Celery worker connect within ~10s. If it loops on
"Cannot connect to redis://" the secret value is wrong or the Upstash
instance isn't reachable from the iad region.

## Production

Provision a second Upstash instance for the production Fly app when we
cut over. Don't share Redis between staging and production — the
broker queues would collide.

## Health check from a running app

The API exposes a readiness check that probes Redis as a hard
dependency. Hit:

```bash
curl https://linda-staging.fly.dev/api/v1/ready | jq '.checks[] | select(.name=="redis")'
```

The endpoint returns 503 when Redis is unreachable (which removes the
machine from the load balancer until Redis comes back). The
`checks[]` array surfaces the per-probe latency and error so
dashboards can tell the difference between "Redis is slow" and "Redis
is down."
