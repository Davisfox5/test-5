"""Item Response Theory (IRT) calibration for QA scorecards.

Fits a 2-parameter-logistic model per scorecard item using the history
of ``InteractionScore`` rows:

    P(correct | θ, a, b) = σ(a · (θ − b))

where θ is an interaction's latent "quality" ability, ``a`` is the
item's discrimination, and ``b`` is its difficulty.  Items with
extremely low ``a`` don't discriminate and should be retired; items with
extreme ``b`` are trivial or unreachable.

The fitter uses alternating maximum-likelihood (EM-style):

1. Initialize θ per interaction from its overall scorecard score.
2. Fit (a, b) per item by logistic regression over θ.
3. Re-estimate θ per interaction from the fitted items.
4. Iterate until parameters stabilize (≤5 iterations typical).

No numpy / scipy dependency — this module is pure Python so it runs in
the existing Celery worker without extra installs.

Results are written back onto ``ScorecardTemplate.criteria`` as
``{a, b, passing_rate, last_fitted_at}`` alongside each criterion.
"""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

logger = logging.getLogger(__name__)


MIN_ITEM_RESPONSES = 30
MIN_THETA_RESPONSES = 3
MAX_ITER = 5
CONVERGENCE_EPS = 1e-3


# ── Data collection ──────────────────────────────────────────────────────


@dataclass
class ItemResponse:
    theta_idx: int   # position in θ vector for the responding interaction
    correct: int     # 0 or 1


def collect_responses(
    session: Session,
    tenant_id: uuid.UUID,
    template_id: uuid.UUID,
) -> Tuple[Dict[str, List[ItemResponse]], List[float]]:
    """Return ``(responses_by_item_id, theta_init)``.

    ``criterion_scores`` is expected to be a list of
    ``{criterion_id, score, passed}`` dicts (the scorecard service's
    existing shape).  We treat ``passed`` as the binary correctness
    signal; when absent, we threshold ``score >= 0.5``.
    """
    from backend.app.models import InteractionScore

    stmt = (
        select(InteractionScore)
        .where(
            InteractionScore.tenant_id == tenant_id,
            InteractionScore.template_id == template_id,
        )
    )
    rows = session.execute(stmt).scalars().all()
    responses: Dict[str, List[ItemResponse]] = {}
    theta_init: List[float] = []
    for idx, row in enumerate(rows):
        theta_init.append(float(row.total_score or 0) / 100.0)
        for crit in row.criterion_scores or []:
            item_id = str(crit.get("criterion_id") or crit.get("id") or "")
            if not item_id:
                continue
            passed = crit.get("passed")
            if passed is None:
                score = crit.get("score", 0)
                try:
                    passed = float(score) >= 0.5
                except (TypeError, ValueError):
                    passed = False
            responses.setdefault(item_id, []).append(
                ItemResponse(theta_idx=idx, correct=int(bool(passed)))
            )
    return responses, theta_init


# ── Fit one item's (a, b) via logistic regression ────────────────────────


def _fit_item(
    responses: Sequence[ItemResponse],
    theta: Sequence[float],
    lr: float = 0.1,
    n_iter: int = 200,
) -> Tuple[float, float, float]:
    """Return ``(a, b, passing_rate)`` for one item.

    Tiny batch GD over ``(log P(y|θ))``.  Returns a=0, b=0 when data is
    too sparse to fit responsibly.
    """
    if len(responses) < MIN_ITEM_RESPONSES:
        return (0.0, 0.0, _pass_rate(responses))
    a = 1.0
    b = 0.0
    for _ in range(n_iter):
        gA = gB = 0.0
        for r in responses:
            th = theta[r.theta_idx]
            z = max(-35.0, min(35.0, a * (th - b)))
            p = 1.0 / (1.0 + math.exp(-z))
            err = p - r.correct
            gA += err * (th - b)
            gB += err * (-a)
        n = len(responses)
        a -= lr * (gA / n)
        b -= lr * (gB / n)
    return (round(a, 4), round(b, 4), _pass_rate(responses))


def _pass_rate(responses: Sequence[ItemResponse]) -> float:
    if not responses:
        return 0.0
    return round(sum(r.correct for r in responses) / len(responses), 4)


# ── Full EM loop ─────────────────────────────────────────────────────────


@dataclass
class IRTFitResult:
    template_id: uuid.UUID
    item_params: Dict[str, Dict[str, float]]
    n_items_fitted: int
    n_responses: int
    retired_items: List[str]


def fit_template(
    session: Session,
    tenant_id: uuid.UUID,
    template_id: uuid.UUID,
) -> IRTFitResult:
    """Fit 2PL parameters for every item on one scorecard template."""
    responses, theta = collect_responses(session, tenant_id, template_id)
    if not theta or not responses:
        return IRTFitResult(
            template_id=template_id,
            item_params={},
            n_items_fitted=0,
            n_responses=0,
            retired_items=[],
        )

    # EM-style iteration: alternate item fit and θ re-estimation.
    item_params: Dict[str, Dict[str, float]] = {}
    prev_theta = list(theta)
    for iteration in range(MAX_ITER):
        item_params = {}
        for item_id, rs in responses.items():
            a, b, pass_rate = _fit_item(rs, theta)
            item_params[item_id] = {"a": a, "b": b, "passing_rate": pass_rate}
        theta = _reestimate_theta(responses, item_params, n_people=len(theta))
        delta = max(abs(x - y) for x, y in zip(theta, prev_theta)) if theta else 0.0
        prev_theta = list(theta)
        if delta < CONVERGENCE_EPS:
            break

    # Identify items that don't discriminate: |a| < 0.3 on enough responses.
    retired: List[str] = []
    for item_id, params in item_params.items():
        if abs(params["a"]) < 0.3 and len(responses.get(item_id, [])) >= MIN_ITEM_RESPONSES:
            retired.append(item_id)

    result = IRTFitResult(
        template_id=template_id,
        item_params=item_params,
        n_items_fitted=sum(1 for p in item_params.values() if p["a"] != 0.0),
        n_responses=sum(len(r) for r in responses.values()),
        retired_items=retired,
    )
    _write_back(session, template_id, item_params)
    return result


def _reestimate_theta(
    responses: Dict[str, List[ItemResponse]],
    item_params: Dict[str, Dict[str, float]],
    *,
    n_people: int,
    lr: float = 0.2,
    n_iter: int = 50,
) -> List[float]:
    """EAP-ish θ estimate by ML over item responses.  Clamp to [-3, 3]."""
    theta = [0.0] * n_people
    for _ in range(n_iter):
        grad = [0.0] * n_people
        for item_id, rs in responses.items():
            params = item_params.get(item_id)
            if not params or params["a"] == 0:
                continue
            a, b = params["a"], params["b"]
            for r in rs:
                th = theta[r.theta_idx]
                z = max(-35.0, min(35.0, a * (th - b)))
                p = 1.0 / (1.0 + math.exp(-z))
                grad[r.theta_idx] += a * (p - r.correct)
        for i in range(n_people):
            theta[i] -= lr * grad[i]
            theta[i] = max(-3.0, min(3.0, theta[i]))
    return theta


def _write_back(
    session: Session,
    template_id: uuid.UUID,
    item_params: Dict[str, Dict[str, float]],
) -> None:
    """Write fitted (a, b, passing_rate) back onto ScorecardTemplate.criteria."""
    from backend.app.models import ScorecardTemplate

    template = session.query(ScorecardTemplate).filter(
        ScorecardTemplate.id == template_id
    ).first()
    if template is None:
        return
    now = datetime.now(timezone.utc).isoformat()
    criteria = list(template.criteria or [])
    for crit in criteria:
        cid = str(crit.get("id") or crit.get("criterion_id") or "")
        if cid and cid in item_params:
            crit.setdefault("irt", {})
            crit["irt"].update({**item_params[cid], "last_fitted_at": now})
    template.criteria = criteria
    flag_modified(template, "criteria")
    session.commit()


def fit_all_templates_for_tenant(
    session: Session,
    tenant_id: uuid.UUID,
) -> List[IRTFitResult]:
    from backend.app.models import ScorecardTemplate

    templates = session.query(ScorecardTemplate).filter(
        ScorecardTemplate.tenant_id == tenant_id
    ).all()
    return [fit_template(session, tenant_id, t.id) for t in templates]
