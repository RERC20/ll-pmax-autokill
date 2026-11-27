# surf/sales.py - true Testing sales per ORDER (createdAt + Testing share in GBP pennies), using exactly the
# stores-monitor hourly.py rules: same order exclusions (test, cancelled, old-order copies, imported) and the same
# Testing / Winners line split (a line is Winners if its product had >= 1 earlier auto-kill sale, else Testing; the
# order total is split over its lines by line value). No customer data is kept.
import datetime as dt

from . import hourly


def testing_orders(shop, a_utc, b_utc, drop_store_credit=False):
    """[(created_utc, testing_pennies_gbp)] for orders created in [a_utc, b_utc).
    drop_store_credit (rules v3): store-credit orders (hourly.credit_reason == "store_credit") are left out too."""
    orders = hourly._pull(shop, f"created_at:>='{hourly._iso_utc(a_utc)}' created_at:<'{hourly._iso_utc(b_utc)}'")
    cfg = hourly.split_cfg()
    h_orders, _ = hourly.history_orders(shop, cfg["cutover"])
    hist = hourly.History(h_orders, cfg["cutover"]).add(orders)      # the window pull is fresher than the history
    out = []
    for o in orders:
        if not (a_utc <= o["created"] < b_utc) or hourly.total_exclusion(o):
            continue
        if drop_store_credit and hourly.credit_reason(o) == "store_credit":
            continue
        sp, _ = hourly.split_order(o, hist, cfg["min_prior"])
        out.append((o["created"].astimezone(dt.timezone.utc), sp["testing"]["p"]))
    return sorted(out)


def gbp_usd(uk_date):
    """(rate, source) GBP->USD for a UK date, same ECB source as hourly.py / the daily PnL."""
    rates, src = hourly.fx_rates(uk_date, uk_date)
    v = rates.get(uk_date.isoformat(), {}).get("gbp_usd")
    return (v[0] if v else None), src.get("gbp_usd")
