"""RiskManager: runs the fixed rule chain. Pure code — the LLM cannot change or bypass it."""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel

from papermind.core.money import ZERO, round_to_step
from papermind.core.types import OrderRequest
from papermind.risk.rules import RULES, RiskContext, RuleResult


class RiskDecision(BaseModel):
    approved: bool
    failed_rule_id: str | None
    message: str
    results: list[RuleResult]
    risk_amount: Decimal | None = None
    est_charges: Decimal | None = None


class RiskManager:
    def evaluate(self, ctx: RiskContext, req: OrderRequest) -> RiskDecision:
        results: list[RuleResult] = []
        for rule in RULES:
            try:
                res = rule(ctx, req)
            except Exception as exc:  # fail safe: an erroring rule is a rejection
                res = RuleResult(rule_id=rule.__name__.upper(), passed=False, message=f"rule error: {exc}")
            results.append(res)
            if not res.passed:
                return RiskDecision(approved=False, failed_rule_id=res.rule_id, message=res.message, results=results)
        return RiskDecision(
            approved=True,
            failed_rule_id=None,
            message="all risk rules passed",
            results=results,
            risk_amount=ctx.risk_amount(req),
            est_charges=ctx.est_charges(req),
        )

    def max_qty_for_risk(self, ctx: RiskContext, req: OrderRequest) -> Decimal:
        """Largest qty (multiple of step and lot) whose risk incl. charges fits the per-trade cap."""
        inst = ctx.instrument
        cap = ctx.max_risk_allowed()
        entry = ctx.est_entry(req)
        if inst is None or cap is None or entry is None or req.stop_loss is None:
            return ZERO
        dist = abs(entry - req.stop_loss) * inst.contract_size
        if dist <= 0:
            return ZERO
        step = inst.qty_step  # lot-based markets set qty_step = lot size
        hi_units = int(cap / (dist * step)) + 1
        lo, hi = 0, hi_units
        while lo < hi:  # binary search on number of steps
            mid = (lo + hi + 1) // 2
            q = step * mid
            risk = ctx.risk_amount(req, q)
            if risk is not None and risk <= cap:
                lo = mid
            else:
                hi = mid - 1
        qty = round_to_step(step * lo, step, "down")
        return qty if qty >= inst.min_qty else ZERO
