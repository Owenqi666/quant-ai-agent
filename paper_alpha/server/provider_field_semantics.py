"""Allowlisted metadata semantics shared by both daily synthetic context paths."""
from __future__ import annotations

from ..evaluation import AVAILABILITY, FIELDS


def _declared(metadata, key):
    value = metadata.get(key)
    return value if isinstance(value, str) and value.strip() else None


def _description(metadata, key, label):
    value = _declared(metadata, key)
    return f'{label} declared by metadata.{key}: {value}.' if value is not None else f'{label}: undeclared.'


def synthetic_provider_fields(metadata):
    """Project declarations, never infer market units or expose the full metadata.

    The caller binds this metadata to its verified dataset digest. Missing units
    stay null. This helper does not qualify real datasets or alter calculations.
    """
    if metadata.get('data_kind') != 'synthetic' or metadata.get('field_availability') != AVAILABILITY:
        raise ValueError('Only the registered daily synthetic OHLCV contract is supported')
    fields = []
    for name in FIELDS:
        key = 'volume_units' if name == 'volume' else 'price_units'
        definition = f'Registered synthetic daily {name} observation. ' + _description(metadata, key, 'Units')
        if name != 'volume':
            definition += ' ' + _description(metadata, 'adjustment', 'Price adjustment')
        definition += ' ' + _description(metadata, 'calendar_kind', 'Calendar')
        fields.append({'name': name, 'kind': 'raw', 'available_at': AVAILABILITY[name],
                       'unit': _declared(metadata, key), 'definition': definition})
    fields.append({'name': 'returns', 'kind': 'derived', 'available_at': 'after session close',
                   'unit': 'dimensionless',
                   'definition': 'close[t] / close[t-1] - 1 on declared sessions; no fill; first row is missing. '
                                 + _description(metadata, 'adjustment', 'Underlying close price adjustment')})
    return fields


def provider_catalog_names(context):
    """Read a separate verified declared-daily catalog without widening v1."""
    if context.get('catalog', {}).get('family') == 'declared_daily':
        from .provider_catalog import validate_catalog
        catalog = validate_catalog(context['catalog'])
        public = catalog['public_projection']
        if context['capabilities'] != public['declared_capabilities']:
            raise ValueError('Declared context capability differs from frozen catalog')
        fields = {f['name'] for f in public['declared_capabilities']['fields']}
        operators = {o['name'] for o in public['operators']} | set(public['documented_aliases'])
        return fields, operators
    cap = context['capabilities']
    return {f['name'] for f in cap['fields']}, {o['name'] for o in cap['operators']} | set(cap['documented_aliases'])
