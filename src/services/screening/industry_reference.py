"""Versioned canonical industry references; names are display-only.

``industry_taxonomy`` is a versioned namespace (e.g. capco_major_v1).
Adapters must supply a published/effective as-of and immutable evidence id.
This structural guard cannot authenticate a provider assertion on its own.
"""
from datetime import date
import re

INDUSTRY_FIELDS = ('industry', 'industry_taxonomy', 'industry_code', 'industry_as_of',
                   'industry_source', 'industry_evidence_id')


def industry_identity(reference, signal_date):
    get = reference.get if hasattr(reference, 'get') else lambda key: getattr(reference, key, None)
    if any(not isinstance(get(key), str) or not get(key).strip() for key in INDUSTRY_FIELDS):
        return ''
    try:
        day = date.fromisoformat(str(signal_date))
        as_of = date.fromisoformat(get('industry_as_of'))
    except (ValueError, TypeError):
        return ''
    taxonomy, code = get('industry_taxonomy'), get('industry_code')
    if (as_of > day or not re.fullmatch(r'[a-z][a-z0-9_]*_v[0-9]+', taxonomy)
            or not re.fullmatch(r'[A-Za-z0-9._-]+', code)):
        return ''
    if taxonomy == 'capco_major_v1' and not re.fullmatch(r'[A-Z][0-9]{2}', code):
        return ''
    return taxonomy + ':' + code
