# Multi-region + DR plan

Forward-looking design doc. We run single-region (us-east-1) today; this
captures the path to multi-region when customer demand or a compliance
requirement forces it. Nothing here is implemented yet — the doc is here
so the next engineer doesn't have to rediscover the shape.

## Current footprint (single region)

- Primary: AWS us-east-1
- Postgres: Neon (managed, pooled)
- Redis: Upstash / ElastiCache
- Qdrant: managed cluster, same region
- Object storage: S3, same region
- Workers: same region as the API
- Providers that don't live in our region and are outside our failover
  scope: Deepgram, Anthropic, HubSpot / Salesforce / Pipedrive. These are
  dependencies, not data.

## What "multi-region" means for LINDA

**Data plane**: every tenant's data lives in a home region. Write traffic
goes there; read traffic can optionally be served from a read replica in
a secondary region for latency-sensitive dashboards. We do **not** plan to
write the same row in two regions — the tenant-home-region model keeps GDPR
residency simple and avoids distributed-write consistency pain.

**Control plane**: API replicas in every region. A tenant's requests get
routed by a header (`X-Tenant-Region`) or by slug-based DNS
(`acme.us.linda.example.com` vs `acme.eu.linda.example.com`). The API talks
to the correct regional Postgres.

**Failover**: we can bring up the secondary region in read-only mode in
under an hour, and in read-write mode once the primary's Postgres has been
confirmed unrecoverable (typically hours, not minutes — RPO > 15 min is
acceptable for voice analytics).

## RTO / RPO targets

| Tier | RTO | RPO | Notes |
|------|-----|-----|-------|
| Read API (dashboards) | 30 min | 15 min | Replica-served; acceptable stale data. |
| Write API (ingest) | 2 h | 15 min | Tenants paused during cutover; ingest queue drained after. |
| Live telephony | n/a | n/a | Active calls drop on failover; callers redial, hit the new region. |

Current single-region posture: RTO is dictated by Neon's recovery SLA
(typically 15 min for a replica, hours for a full region-out); RPO is the
WAL-ship lag (< 1 min).

## Things we'd need to build

1. **Tenant-home-region column** on `tenants`. Defaults to us-east-1.
2. **Region-aware Postgres pool**: the app reads `tenant.home_region` on
   every request and picks the right connection pool. Straightforward once
   the column exists.
3. **Cross-region Redis replication** for Celery — or per-region workers,
   which is probably cleaner. Tasks for tenant X run in tenant X's region.
4. **Replica read routing**: a `?stale=ok` query param that lets the API
   serve from the secondary region's read replica for dashboards.
5. **DNS**: Route 53 weighted records. Failover policy is
   `primary → replica` with a healthcheck on `/ready/deep`.
6. **S3 cross-region replication** on the staging bucket for audio
   uploads; the recording URL signing logic needs to understand which
   region the object landed in.
7. **Deploy pipeline**: the CI job already builds a single image tag;
   deploy step needs to fan out to every region's deploy hook.

## Cost of doing nothing

Single region + a weekly backup (already in scripts/tasks) covers
"ransomware" and "accidentally-deleted-data" scenarios but not "AWS region
outage." For a B2B SaaS with 24-hour recovery tolerance, that's defensible
until the first customer asks for EU data residency — which is when we flip
this into a project.

## Near-term DR posture

Until multi-region is built, the DR plan is:

1. Nightly `tenant_backup_all_tenants` writes every tenant's data to S3
   (see scripts/tasks and §7 of the runbook).
2. S3 versioning + lifecycle keeps backups for 30 days.
3. Postgres point-in-time recovery via Neon covers the last 7 days.
4. If us-east-1 goes dark:
   - Spin up the infra template in us-west-2 (Terraform in a separate repo).
   - Restore Postgres from Neon's cross-region snapshot.
   - Replay the latest S3 backup bundle with
     `scripts/restore_from_backup.py <s3-key>` (wraps
     `tenant_restore_from_s3`).
   - Point DNS at the new region.
   - Estimated time: 4 hours; fits the "T+1 business day" SLA we publish
     to customers today.

## Decision triggers

Revisit this plan when any of the following fires:

- A customer writes multi-region residency into their contract.
- Any tenant tops 10 TB of persisted data (restore from backup becomes
  painful).
- Regulatory — SOC 2 Type II, HIPAA, or equivalent — requires a documented
  DR exercise.
- AWS us-east-1 suffers a > 4 hour partial outage that affects us.
