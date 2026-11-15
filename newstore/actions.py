# newstore/actions.py - Shopify writes for the new-store rules. Only called when the run is LIVE (NS_LIVE=1 + --live).
# Tags + the custom_label_1 metafield (mm-google-shopping, as the old engine writes it) + drafts. No Google Ads writes:
# while the Winners / Last Chance campaign ids are empty nothing is moved through listing trees.
from . import config as C

_TAGS = ('mutation($id:ID!,$add:[String!]!,$rm:[String!]!){tagsAdd(id:$id,tags:$add){userErrors{message}} '
         'tagsRemove(id:$id,tags:$rm){userErrors{message}}}')
_ADD = 'mutation($id:ID!,$add:[String!]!){tagsAdd(id:$id,tags:$add){userErrors{message}}}'
_RM = 'mutation($id:ID!,$rm:[String!]!){tagsRemove(id:$id,tags:$rm){userErrors{message}}}'
_LABEL = 'mutation($mf:[MetafieldsSetInput!]!){metafieldsSet(metafields:$mf){userErrors{message}}}'
_DRAFT = ('mutation($id:ID!,$add:[String!]!){productUpdate(input:{id:$id,status:DRAFT}){userErrors{message}} '
          'tagsAdd(id:$id,tags:$add){userErrors{message}}}')


def _gid(pid):
    return f'gid://shopify/Product/{pid}'


def _ok(j, *fields):
    if j.get('errors'):
        return f"err: {str(j['errors'])[:100]}"
    d = j.get('data') or {}
    errs = sum([((d.get(f) or {}).get('userErrors') or []) for f in fields], [])
    return 'ok' if not errs else f'err: {str(errs)[:100]}'


def set_label(shop, pid, value):
    return _ok(shop.gql(_LABEL, {'mf': [{'ownerId': _gid(pid), 'namespace': C.LABEL_NAMESPACE, 'key': C.LABEL_KEY,
                                         'type': 'single_line_text_field', 'value': value}]}), 'metafieldsSet')


def tags(shop, pid, add, rm):
    add, rm = [t for t in add if t], [t for t in rm if t and t not in add]
    if add and rm:
        return _ok(shop.gql(_TAGS, {'id': _gid(pid), 'add': add, 'rm': rm}), 'tagsAdd', 'tagsRemove')
    if add:
        return _ok(shop.gql(_ADD, {'id': _gid(pid), 'add': add}), 'tagsAdd')
    if rm:
        return _ok(shop.gql(_RM, {'id': _gid(pid), 'rm': rm}), 'tagsRemove')
    return 'ok'


def draft(shop, pid, rm=()):
    pub = ((shop.read('{product(id:"%s"){publishedAt}}' % _gid(pid)).get('product') or {}).get('publishedAt'))
    add = [C.DRAFT_TAG] + (['pub:' + str(pub)[:10]] if pub else [])      # Shopify clears publishedAt on draft: keep it
    r = _ok(shop.gql(_DRAFT, {'id': _gid(pid), 'add': add}), 'productUpdate', 'tagsAdd')
    return tags(shop, pid, [], list(rm)) if (r == 'ok' and rm) else r


def apply(shop, d, product, run):
    """Write one decision. Label first: feed routing follows custom_label_1, so a half-done move is repaired next run."""
    today = run.isoformat()
    old_lc = [t for t in product.tags if t.startswith(C.LC_STAMP)]
    old_w = [t for t in product.tags if t.startswith(C.W_STAMP)]
    if d.rule == 'first_sale':
        r = set_label(shop, d.pid, C.WINNER_TAG)
        return r if r != 'ok' else tags(shop, d.pid, [C.WINNER_TAG, C.W_STAMP + d.detail['last_sale']], old_w)
    if d.rule in ('weak_seller', 'zombie'):
        r = set_label(shop, d.pid, C.LC_TAG)
        return r if r != 'ok' else tags(shop, d.pid, [C.LC_TAG, C.LOST_TAG, C.LC_STAMP + today],
                                        [C.WINNER_TAG] + old_lc + old_w)
    if d.rule == 'lc_return':
        r = set_label(shop, d.pid, C.WINNER_TAG)
        grad = [t for t in product.tags if t.startswith('lc_grad:')]
        return r if r != 'ok' else tags(shop, d.pid, [C.WINNER_TAG, C.W_STAMP + d.detail['rescue_sale']],
                                        [C.LC_TAG, C.LOST_TAG] + old_lc + old_w + grad)
    if d.rule == 'never_sold':
        return draft(shop, d.pid)
    if d.rule == 'lc_draft':
        return draft(shop, d.pid, rm=[C.LC_TAG])
    return f'err: unknown rule {d.rule}'
