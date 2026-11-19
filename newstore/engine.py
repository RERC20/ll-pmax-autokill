#!/usr/bin/env python3
# newstore/engine.py - the new-store auto-kill run (gwvmrw-vm + Google Ads 778). DRY by default.
#
#   python -m newstore.engine                     # dry run: reads live data, prints what each rule WOULD do
#   python -m newstore.engine --json out.json     # ... and saves the full plan
#   NS_LIVE=1 python -m newstore.engine --live    # writes tags / labels / drafts (both switches needed)
#
# Credentials (env or _secrets_local.py, never in this repo): GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN,
# GOOGLE_DEVELOPER_TOKEN, NS_SHOPIFY_CLIENT_ID, NS_SHOPIFY_CLIENT_SECRET (an app installed on gwvmrw-vm).
# Schedule: config.SCHEDULE_MINUTES (30) -> Railway cron config.RAILWAY_CRON. Google Ads ops per run: 3.
import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from creds import cred                                    # noqa: E402  (env first, then _secrets_local.py)

from . import actions, config as C, data, notify, rules   # noqa: E402

EXAMPLES = 10


def run(run_date=None, live=False, json_out=None, fx_raw=None):
    now_uk = datetime.datetime.now(data.UK)
    run_date = run_date or now_uk.date()
    live = live and C.LIVE
    print(f"== new-store engine == {run_date} | store {C.SHOPIFY_STORE} | Ads {C.GOOGLE_CUSTOMER_ID} | "
          f"{'LIVE' if live else 'DRY RUN (nothing written)'} | every {C.SCHEDULE_MINUTES} min")
    print(f"   campaigns counted: {C.campaign_ids()} | routing: tag + custom_label_1 metafield -> feed label -> "
          f"Winners/Last Chance listing filters (no listing-tree item moves)")
    print(f"   new-setup sales only: processed >= {C.CUTOVER}, old-order copies ignored | zombie clock: "
          f"{'from Winners launch ' + C.WINNERS_LAUNCH_DATE if C.WINNERS_LAUNCH_DATE else 'OFF (Winners never enabled)'}")
    ads = data.Ads(cred)
    data.check_account(ads)
    spend, clicks, sinfo = data.fetch_spend(ads, run_date, fx_raw)
    print(f"   spend: {sinfo['rows']} product-day rows, USD {sinfo['usd_total']:.2f} total, "
          f"1 USD = {C.STORE_CURRENCY} {sinfo['fx_latest']:.4f} ({'ECB' if C.STORE_CURRENCY != C.ACCOUNT_CURRENCY else 'same currency'}) | Google Ads ops this run: {ads.run_ops}")
    shop = data.Shop(cred)
    products = data.fetch_products(shop)
    orders = data.fetch_orders(shop)
    sales = data.sales_by_product(orders)
    n_sales = sum(len(v) for v in sales.values())
    excl = sum(1 for o in orders if data.is_excluded_order(o))
    copies = sum(1 for o in orders if data.is_old_store_copy(o))
    credit = [(o['name'], data.credit_reason(o)) for o in orders if not data.is_old_store_copy(o) and data.credit_reason(o)]
    print(f"   shopify: {len(products)} active products | {len(orders)} orders processed since {C.ORDERS_SINCE} "
          f"({excl} cancelled/test, {copies} old-store copies, {len(credit)} store-credit - ignored) | NEW-setup sales: {n_sales} on {len(sales)} products")
    for name, why in credit:
        print(f"   store-credit order ignored: {name} ({why})")
    if not products or not orders:
        raise RuntimeError('products or orders pull returned 0 - data glitch, nothing judged')
    roles = {r: sum(1 for p in products if rules.role(p) == r) for r in ('testing', 'winner', 'last_chance')}
    print(f"   roles: {roles}")

    decisions = rules.evaluate(products, sales, spend, clicks, run_date)
    allowed, blocked = rules.apply_caps(decisions)
    by_pid = {p.pid: p for p in products}
    report = {'run_date': run_date.isoformat(), 'live': live, 'store': C.SHOPIFY_STORE, 'account': C.GOOGLE_CUSTOMER_ID,
              'campaigns': C.campaign_ids(), 'spend': sinfo, 'roles': roles, 'google_ops_this_run': ads.run_ops,
              'rules': {}}
    for r in rules.RULES:
        ds = [d for d in decisions if d.rule == r]
        report['rules'][r] = {'action': rules.ACTION[r], 'count': len(ds), 'cap': C.CAPS[r],
                              'blocked_by_cap': r in blocked,
                              'examples': [{'handle': d.handle, 'pid': d.pid, 'reason': d.reason} for d in ds[:EXAMPLES]],
                              'all': [{'handle': d.handle, 'pid': d.pid, 'reason': d.reason, **d.detail} for d in ds]}
        flag = f'  !! {len(ds)} > cap {C.CAPS[r]}: NONE written this run' if r in blocked else ''
        print(f"\n-- {r} -> {rules.ACTION[r]}: {len(ds)}{flag}")
        for d in ds[:EXAMPLES]:
            print(f"     {d.handle[:70]} | {d.reason}")
    results = {}
    if live:
        for d in allowed:
            results[d.pid] = actions.apply(shop, d, by_pid[d.pid], run_date)
            print(f"   write {d.rule} {d.handle[:50]} -> {results[d.pid]}")
    report['writes'] = results
    if json_out:
        with open(json_out, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=1, default=str)
        print(f"\nplan saved -> {json_out}")
    return report


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--live', action='store_true', help='write (also needs NS_LIVE=1)')
    ap.add_argument('--run-date')
    ap.add_argument('--json')
    ap.add_argument('--no-telegram', action='store_true', help='do not send the Telegram run report')
    a = ap.parse_args(argv)
    if a.live and not C.LIVE:
        print('--live given but NS_LIVE is not 1: running DRY')
    tg = C.TELEGRAM and not a.no_telegram
    try:
        report = run(datetime.date.fromisoformat(a.run_date) if a.run_date else None, a.live, a.json)
    except Exception as ex:
        if tg:
            notify.send(cred, notify.failure(ex))
        raise
    if tg:
        notify.send(cred, notify.summary(report))


if __name__ == '__main__':
    main()
