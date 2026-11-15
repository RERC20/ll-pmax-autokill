# newstore/rules.py - the owner's CONFIRMED rules for the new store (2026-11-15). Pure functions: no network, no writes.
#
#   1 FIRST SALE   Testing product (no w_campaign / lc_campaign tag) that has sold once -> w_campaign (+ label metafield).
#   2 NEVER SOLD   Testing product with 0 sales: spend > min(price / 7, GBP 5) or 25+ clicks -> draft. Checked every run.
#   3 WEAK SELLER  Winner (from its 1st sale / Winners entry):
#                    a. no sale in the last 7 days  AND spend in the last 7 days  >= price / 2 -> lc_campaign
#                    b. else no sale in the last 14 days AND spend in the last 14 days >= price / 2 -> lc_campaign
#                    c. else spend in the (up to) 30 days since its last sale >= price / 2 (no sale since) -> lc_campaign
#   4 ZOMBIE       Winner with no new sale for 30 days since the LATER of its last sale / Winners entry and the day the
#                  Winners campaign was first enabled (config.WINNERS_LAUNCH_DATE; empty = never = no zombie moves).
# Every rule sees NEW-SETUP sales only (data.sales_by_product: processed >= cutover, no old-order copies, no
# store-credit orders - see config CREDIT_* / MIN_REVENUE_SHARE). Products
# tagged was_w_campaign / was_lc_campaign are ordinary Testing products (history tags, never read here).
#   5 LAST CHANCE  a new sale (after the lc: stamp) -> back to w_campaign;
#                  spend since entry > min(price / 2, GBP 20) with no sale -> draft.
# REMOVED vs the old engine: winner pace, the 14-day Winners trial; the 7/30-day ROAS < 2.4 draft rule is replaced by 3.
#
# Windows: "the last N days" = the N UK calendar days ending today (today included), as in the old engine. A product's
# Winners ANCHOR is the later of its last sale and its Winners entry stamp; "no sale in the last N days" means the anchor
# is at least N days before today. Spend is GBP per UK day (already converted from USD and aligned from New York days).
import datetime
from dataclasses import dataclass, field

from . import config as C

RULES = ('first_sale', 'never_sold', 'weak_seller', 'zombie', 'lc_return', 'lc_draft')
ACTION = {'first_sale': 'tag_winner', 'never_sold': 'draft', 'weak_seller': 'to_last_chance',
          'zombie': 'to_last_chance', 'lc_return': 'back_to_winners', 'lc_draft': 'draft_last_chance'}


@dataclass
class Product:
    pid: str
    handle: str
    title: str
    price: float                     # GBP (lowest variant price, as the old engine)
    tags: set = field(default_factory=set)
    status: str = 'ACTIVE'


@dataclass
class Decision:
    rule: str
    pid: str
    handle: str
    reason: str
    detail: dict = field(default_factory=dict)

    @property
    def action(self):
        return ACTION[self.rule]


def _d(s):
    return datetime.date.fromisoformat(s)


def stamp_date(tags, *prefixes):
    """Newest YYYY-MM-DD among tags starting with any prefix (None if none parse)."""
    out = []
    for t in tags:
        for p in prefixes:
            if t.startswith(p):
                try:
                    out.append(_d(t[len(p):len(p) + 10]))
                except ValueError:
                    pass
    return max(out) if out else None


def spend_between(daily, d0, d1):
    """Sum of a {uk_date: value} map over d0..d1 inclusive (empty when d0 > d1)."""
    if d0 > d1:
        return 0.0
    return sum(v for d, v in daily.items() if d0 <= d <= d1)


def role(p):
    if C.LC_TAG in p.tags:
        return 'last_chance'
    if C.WINNER_TAG in p.tags:
        return 'winner'
    return 'testing'


def winners_anchor(p, sales):
    """Later of the last sale and the Winners entry stamp (w_since: / lc_grad:); fallback = the store-move day."""
    last = max(sales) if sales else None
    entry = stamp_date(p.tags, C.W_STAMP, 'lc_grad:')
    cands = [d for d in (last, entry) if d]
    return (max(cands) if cands else C.WINNERS_ANCHOR_FALLBACK), last, entry


def eval_testing(p, sales, spend, clicks, run):
    if sales:
        first = min(sales)
        return Decision('first_sale', p.pid, p.handle, f'sold {len(sales)}x (first {first}, last {max(sales)})',
                        {'sales': len(sales), 'first_sale': first.isoformat(), 'last_sale': max(sales).isoformat()})
    d0 = run - datetime.timedelta(days=C.NEVER_SOLD_LOOKBACK_D - 1)
    sp = spend_between(spend, d0, run)
    ck = spend_between(clicks, d0, run)
    allow = min(p.price / C.NEVER_SOLD_PRICE_DIV, C.NEVER_SOLD_CAP_GBP) if p.price > 0 else C.NEVER_SOLD_CAP_GBP
    if sp > allow or ck >= C.NEVER_SOLD_CLICKS:
        why = (f'spend GBP {sp:.2f} > GBP {allow:.2f}' if sp > allow else f'{ck:.0f} clicks >= {C.NEVER_SOLD_CLICKS}')
        return Decision('never_sold', p.pid, p.handle, f'never sold; {why} ({C.NEVER_SOLD_LOOKBACK_D}d)',
                        {'spend_gbp': round(sp, 2), 'clicks': round(ck, 1), 'allowance_gbp': round(allow, 2)})
    return None


def eval_winner(p, sales, spend, run):
    anchor, last, entry = winners_anchor(p, sales)
    det = {'anchor': anchor.isoformat(), 'last_sale': last and last.isoformat(), 'entry_stamp': entry and entry.isoformat(),
           'price': p.price}
    if p.price > 0:
        half = p.price / C.WEAK_PRICE_DIV
        for n in C.WEAK_WINDOWS_D:                                   # 3a, 3b
            if anchor <= run - datetime.timedelta(days=n):
                sp = spend_between(spend, run - datetime.timedelta(days=n - 1), run)
                if sp >= half:
                    return Decision('weak_seller', p.pid, p.handle,
                                    f'no sale in {n}d (anchor {anchor}); {n}d spend GBP {sp:.2f} >= price/2 GBP {half:.2f}',
                                    {**det, 'window': f'{n}d', 'spend_gbp': round(sp, 2), 'threshold_gbp': round(half, 2)})
        if anchor < run:                                             # 3c: the (up to) 30 days after the anchor
            end = min(anchor + datetime.timedelta(days=C.WEAK_SINCE_SALE_D), run)
            sp = spend_between(spend, anchor + datetime.timedelta(days=1), end)
            if sp >= half:
                return Decision('weak_seller', p.pid, p.handle,
                                f'no sale since {anchor}; spend since GBP {sp:.2f} >= price/2 GBP {half:.2f}',
                                {**det, 'window': f'since-anchor(<= {C.WEAK_SINCE_SALE_D}d)', 'spend_gbp': round(sp, 2),
                                 'threshold_gbp': round(half, 2)})
    launch = C.winners_launch()                                      # 4: no zombie moves before Winners is enabled
    if launch is None:
        return None
    zanchor = max(anchor, launch)
    days = (run - zanchor).days
    if days >= C.ZOMBIE_D:
        return Decision('zombie', p.pid, p.handle, f'no sale for {days}d (since {zanchor}; Winners launched {launch})',
                        {**det, 'zombie_anchor': zanchor.isoformat(), 'days_without_sale': days})
    return None


def eval_last_chance(p, sales, spend, run):
    entry = stamp_date(p.tags, C.LC_STAMP) or C.LC_ANCHOR_FALLBACK
    post = sorted(d for d in sales if d > entry)                     # only sales AFTER the stamp day (old engine rule)
    if post:
        return Decision('lc_return', p.pid, p.handle, f'sale on {post[0]} after Last Chance entry {entry}',
                        {'lc_entry': entry.isoformat(), 'rescue_sale': post[0].isoformat()})
    sp = spend_between(spend, entry + datetime.timedelta(days=1), run)
    allow = min(p.price / C.LC_PRICE_DIV, C.LC_CAP_GBP) if p.price > 0 else C.LC_CAP_GBP
    if sp > allow:
        return Decision('lc_draft', p.pid, p.handle, f'LC spend GBP {sp:.2f} > GBP {allow:.2f}, no sale since {entry}',
                        {'lc_entry': entry.isoformat(), 'spend_gbp': round(sp, 2), 'allowance_gbp': round(allow, 2)})
    return None


def evaluate(products, sales, spend, clicks, run):
    """products: [Product]; sales: {pid: [uk dates]}; spend/clicks: {pid: {uk_date: float}}; run: UK date.
    Returns [Decision] - at most one per product. Non-active products are never judged."""
    out = []
    for p in products:
        if p.status != 'ACTIVE':
            continue
        s = sorted(d for d in sales.get(p.pid, ()) if d <= run)
        sp = spend.get(p.pid, {})
        r = role(p)
        if r == 'testing':
            d = eval_testing(p, s, sp, clicks.get(p.pid, {}), run)
        elif r == 'winner':
            d = eval_winner(p, s, sp, run)
        else:
            d = eval_last_chance(p, s, sp, run)
        if d:
            out.append(d)
    return out


def apply_caps(decisions, caps=None):
    """Glitch guard per rule: over its cap, NONE of that rule's moves are written this run. Returns (allowed, blocked)."""
    caps = caps or C.CAPS
    by = {r: [d for d in decisions if d.rule == r] for r in RULES}
    blocked = {r: len(v) for r, v in by.items() if len(v) > caps.get(r, 0)}
    allowed = [d for d in decisions if d.rule not in blocked]
    return allowed, blocked
