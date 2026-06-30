# CASA Readiness Checklist

LINDA requests the **restricted** scope `gmail.readonly`, and its backend (the
Fly app behind `lindaai.net`) accesses that Gmail data **through a server**.
Per Google, *"every app that requests access to Google users' restricted data
and has the ability to access data from or through a third-party server must go
through a security assessment"*
([Google: restricted-scope verification](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification)).
That assessment uses the **App Defense Alliance (ADA) CASA** framework (Cloud
Application Security Assessment).

This doc maps LINDA's deployment to the CASA process so the lift and cost are
known. **It does not set a schedule** — assessment and Google-review timing are
lab- and Google-dependent and are yours to plan.

---

## 1. What level applies to LINDA

> ⚠️ **Important correction to the original "Tier 2 self-scan" framing.** As of
> 2026, a pure unvalidated self-scan is **not** accepted for restricted scopes
> that touch a server. The assessment must be performed/validated by an **ADA
> Authorized Lab**, which issues a **Letter of Assessment/Validation (LOA)** that
> you submit to Google. There is a lower-cost "self-guided" Tier 2 path, but an
> authorized lab still validates the results and issues the letter.

- **Trigger:** restricted scope (`gmail.readonly`) + server-side data access → mandatory CASA.
- **Assessment type:** primarily a **DAST** (Dynamic Application Security
  Testing) scan of the internet-facing deployment, plus a self-assessment
  questionnaire mapped to the CASA / OWASP ASVS control set, validated by an
  authorized lab.
- **Authorized labs** (examples reported publicly): TAC Security, Leviathan,
  DEKRA, NCC, Bishop Fox, and others on the ADA list. They run/validate the
  scan and report the LOA to Google.
- **Cost (reference only, from public reports):** the self-guided Tier 2 lab
  validation is commonly in the **~$540–$1,000** range; lab-led / pen-test
  engagements cost more. Confirm with the chosen lab — this is not a quote.
  Sources: see bottom.
- **Recertification:** to keep the restricted scope, the security assessment
  must be **redone at least every 12 months** after the LOA approval date.

## 2. What gets scanned

| Item | Value |
|------|-------|
| Primary in-scope origin | `https://lindaai.net` (apex → `linda-staging` Fly app) |
| Equivalent origin | `https://linda-staging.fly.dev` |
| Data-handling surface | The FastAPI backend (`backend/app/...`) — OAuth, email ingest, storage, and the Anthropic analysis path |
| Out of scope for Gmail data | SPA `linda-staging-app` (Clerk auth UI), `linda-marketing` (static site). Note these may still be probed by a DAST crawler. |

The lab will need a reachable test environment and, typically, a test
account/credentials to exercise authenticated paths. Decide whether they scan
`lindaai.net` directly or a dedicated assessment environment.

## 3. Readiness map — CASA control areas vs. LINDA today

CASA draws on OWASP ASVS. The high-signal areas for a DAST + questionnaire, and
where LINDA stands:

| Control area | Status | Notes / evidence |
|--------------|--------|------------------|
| TLS / HTTPS everywhere | ✅ | `force_https = true` in all `fly.toml`; Let's Encrypt cert on `lindaai.net`. |
| Secrets management | ✅ | `GOOGLE_CLIENT_ID/SECRET`, `TOKEN_ENCRYPTION_KEY`, `ANTHROPIC_API_KEY` are Fly secrets, not in repo. |
| Encryption at rest | ✅ | OAuth tokens Fernet-encrypted (`services/token_crypto.py`); attachments S3 SSE-AES256. See [`limited-use-compliance.md`](limited-use-compliance.md) §5. |
| Authn/authz on data endpoints | ✅ | Tenant-scoped auth (`get_current_tenant`); scope decorators on routers. |
| OAuth state/CSRF handling | ✅ | `state` token in Redis with TTL on authorize/callback (`api/oauth.py`). |
| Token revocation & data deletion | ✅ (this branch) | Upstream Google revoke + ingested-mail purge on disconnect; tenant hard-delete purges all. |
| Input validation / injection | ◻️ review | Pydantic models on API; confirm no SQL string-building, template injection, or SSRF in webhook handlers (`email_push.py`). |
| Security headers / CORS | ◻️ review | Confirm CORS allowlist and standard security headers on the API responses. |
| Dependency / known-CVE posture | ◻️ review | DAST is runtime, but labs often ask about dependency hygiene; `requirements.txt` pinned. |
| Rate limiting / abuse controls | ✅ partial | Push webhooks rate-limited (`email_push.py`); confirm coverage on auth endpoints. |
| Logging without sensitive data | ◻️ review | Confirm tokens / mail bodies are never logged (token_crypto logs warnings only, not values). |
| Vuln disclosure / security contact | ◻️ add | Consider a `security@lindaai.net` contact / `/.well-known/security.txt`. |

✅ = in place · ◻️ = verify or add before the lab scan.

## 4. Pre-engagement checklist

- [ ] Pick an ADA authorized lab; get a quote and the questionnaire.
- [ ] Decide the scan target (`lindaai.net` vs. a dedicated assessment env) and
      provide test credentials.
- [ ] Close the ◻️ review items in §3 (headers/CORS, input-validation/SSRF pass
      on webhooks, logging audit, security contact).
- [ ] Ensure the deployment under scan reflects this branch (revoke/purge +
      legal pages merged and deployed).
- [ ] Run a pre-scan with a free DAST tool (e.g. OWASP ZAP) against
      `lindaai.net` to catch easy findings before paying for the lab pass.
- [ ] Complete the CASA self-assessment questionnaire; submit to the lab.
- [ ] Obtain the LOA; submit it in the Google OAuth verification flow.
- [ ] Calendar the **12-month** recertification trigger from the LOA date.

## 5. Sequence (who does what)

1. Public site + legal pages live, Search Console verified, consent screen
   configured, demo video recorded — see
   [`google-oauth-verification/`](google-oauth-verification/).
2. CASA assessment via authorized lab → LOA.
3. Submit verification (consent screen + scopes + video + LOA) to Google.
4. Google OAuth app review.

Steps 2 and 4 each take real calendar time per public reports (independent of
LINDA); plan the order, not a deadline, here.

---

### Sources

- [Google — Restricted scope verification](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification)
- [Google Cloud — Security Assessment (CASA) help](https://support.google.com/cloud/answer/13465431)
- [DeepStrike — Google CASA security assessment overview](https://deepstrike.io/blog/google-casa-security-assessment-2025)
- [BuzzClan — Passing CASA Tier 2](https://buzzclan.com/cyber-security/google-casa-tier-2-assessment/)
- [Orbis — CASA Tier 2 walkthrough](https://meetorbis.com/blog/how-we-passed-google-casa-tier-2-with-claude)

> Cost/time figures above are third-party-reported references for planning
> awareness, not commitments or quotes.
