"""Profit model."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional


@dataclass
class ProfitResult:
    buy_price: float          # projected price paid on Goodwill (before tax)
    total_cost: float         # buy price + tax + Goodwill S&H
    sale_price: float         # expected eBay sale price
    fees: float               # eBay fees
    outbound_shipping: float
    profit: float
    profit_pct: float
    max_bid: float            # highest bid that still meets the target ROI

    def as_db(self) -> Dict[str, Any]:
        return {
            "buy_price": round(self.buy_price, 2),
            "total_cost": round(self.total_cost, 2),
            "est_fees": round(self.fees, 2),
            "est_profit": round(self.profit, 2),
            "profit_pct": round(self.profit_pct, 1),
            "max_bid": round(self.max_bid, 2),
        }

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def projected_buy_price(item: Dict[str, Any], pcfg: Dict[str, Any]) -> float:
    """What we expect to pay on Goodwill (before tax and shipping)."""
    if item.get("listing_type") == "buy_now" and float(item.get("buy_now_price") or 0) > 0:
        return float(item["buy_now_price"])
    current = float(item.get("current_price") or 0)
    minimum = float(item.get("minimum_bid") or 0)
    base = max(current, minimum)
    return base * float(pcfg.get("expected_final_price_multiplier", 1.0))


def ebay_fees(sale_price: float, pcfg: Dict[str, Any]) -> float:
    pct = float(pcfg["ebay_final_value_fee_pct"]) + float(pcfg.get("ebay_promoted_listing_pct", 0))
    return sale_price * pct / 100.0 + float(pcfg["ebay_fixed_fee"])


def compute(buy_price: float, goodwill_shipping: float, sale_price: float,
            pcfg: Dict[str, Any], target_pct: float) -> ProfitResult:
    tax = float(pcfg.get("sales_tax_pct", 0)) / 100.0
    outbound = float(pcfg.get("outbound_shipping_cost", 0))
    fees = ebay_fees(sale_price, pcfg)
    total_cost = buy_price * (1 + tax) + goodwill_shipping
    net = sale_price - fees - outbound
    profit = net - total_cost
    profit_pct = (profit / total_cost * 100.0) if total_cost > 0 else 0.0
    # Solve net - cost = target * cost  ->  cost = net / (1 + target)
    max_cost = net / (1 + target_pct / 100.0)
    max_bid = max(0.0, (max_cost - goodwill_shipping) / (1 + tax))
    return ProfitResult(buy_price, total_cost, sale_price, fees, outbound, profit, profit_pct, max_bid)


def is_deal(result: Optional[ProfitResult], comps: int, pcfg: Dict[str, Any],
            min_comps: int, target_pct: float) -> bool:
    if result is None:
        return False
    return (result.profit_pct >= target_pct
            and result.profit >= float(pcfg.get("min_profit_usd", 0))
            and comps >= min_comps)


def evaluate_row(row: Dict[str, Any], cfg: Dict[str, Any], target_pct: float) -> Dict[str, Any]:
    """Recompute profit columns for a DB row (honours a manual eBay price override)."""
    pcfg = cfg["profit"]
    manual = row.get("ebay_price_manual")
    sale = manual if manual not in (None, "") else row.get("ebay_price")
    if sale in (None, ""):
        return {"is_deal": 0}
    shipping = row.get("goodwill_shipping")
    if shipping in (None, ""):
        shipping = cfg["scraper"]["default_shipping_estimate"]
    res = compute(projected_buy_price(row, pcfg), float(shipping), float(sale), pcfg, target_pct)
    comps = int(row.get("ebay_comps") or 0)
    # A manual price is trusted regardless of comparables count
    min_comps = 0 if manual not in (None, "") else int(cfg["ebay"]["min_comparables"])
    out = res.as_db()
    out["is_deal"] = 1 if is_deal(res, comps, pcfg, min_comps, target_pct) else 0
    return out
