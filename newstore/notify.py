# newstore/notify.py - per-run Telegram report (same bot / chat variables as the old engine: TELEGRAM_BOT_TOKEN,
# TELEGRAM_CHAT_ID). Plain text, split under Telegram's 4,096-character limit. Never raises, never prints the token
# (request errors can contain the URL, which holds the token - only the error TYPE is printed).
import requests

from . import config as C

LIMIT = 4000
EXAMPLES = 5


def parts(text, limit=LIMIT):
    if len(text) <= limit:
        return [text]
    out, cur = [], ''
    for line in text.split('\n'):
        while len(line) > limit:
            if cur:
                out.append(cur); cur = ''
            out.append(line[:limit]); line = line[limit:]
        if cur and len(cur) + 1 + len(line) > limit:
            out.append(cur); cur = line
        else:
            cur = f'{cur}\n{line}' if cur else line
    if cur.strip():
        out.append(cur)
    return out


def summary(report):
    """One short message per run: mode, data, each rule's count (+ up to 5 handles), writes and their results."""
    live = 'LIVE' if report.get('live') else 'DRY RUN'
    sp = report.get('spend') or {}
    lines = [f"Auto-Kill v2 ({C.LABEL}) {report.get('run_date')} | {live}",
             f"Ads {report.get('account')}: spend USD {sp.get('usd_total', 0):.2f} (1 USD = {C.STORE_CURRENCY} {sp.get('fx_latest', 0):.4f}) | "
             f"{report.get('google_ops_this_run', 0)} API ops",
             f"Roles: {report.get('roles')}"]
    total = 0
    for rule, r in (report.get('rules') or {}).items():
        total += r['count']
        flag = ' BLOCKED (over cap, nothing written)' if r.get('blocked_by_cap') else ''
        lines.append(f"- {rule} ({r['action']}): {r['count']}{flag}")
        for e in r['examples'][:EXAMPLES]:
            lines.append(f"    {e['handle'][:60]}")
    w = report.get('writes') or {}
    if w:
        bad = {k: v for k, v in w.items() if v != 'ok'}
        lines.append(f"Writes: {len(w) - len(bad)} ok, {len(bad)} failed" + (f" {list(bad.items())[:3]}" if bad else ''))
    elif total == 0:
        lines.append('Nothing to do this run.')
    return '\n'.join(lines)


def failure(err):
    return f"Auto-Kill v2 ({C.LABEL}) FAILED: {type(err).__name__}: {str(err)[:300]}\nNothing more was written this run."


def send(cred, text, post=requests.post):
    tok, chat = cred('TELEGRAM_BOT_TOKEN'), cred('TELEGRAM_CHAT_ID')
    if not (tok and chat):
        print('telegram: not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) - skipped')
        return False
    ok = True
    for i, part in enumerate(parts(text)):
        try:
            r = post(f'https://api.telegram.org/bot{tok}/sendMessage', data={'chat_id': chat, 'text': part}, timeout=30)
            if r.status_code != 200:
                ok = False
                print(f'telegram: part {i + 1} refused (HTTP {r.status_code})')
        except Exception as ex:                  # noqa: BLE001
            ok = False
            print(f'telegram: part {i + 1} failed ({type(ex).__name__})')
    if ok:
        print('telegram: sent')
    return ok
