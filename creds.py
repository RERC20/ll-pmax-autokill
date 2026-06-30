# creds.py — credential loader. Contains NO secrets; safe to commit.
# Order of lookup for each credential:
#   1. environment variable  (this is how GitHub Actions injects repo Secrets)
#   2. _secrets_local.py      (a git-ignored file with the real values, for LOCAL laptop runs)
#   3. the provided default   (e.g. '' for an intentionally-empty value)
import os

def cred(name, default=''):
    v = os.environ.get(name)
    if v is not None and v != '':
        return v
    try:
        import _secrets_local as _L
        return getattr(_L, name, default)
    except ModuleNotFoundError:
        return default
