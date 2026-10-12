"""Complete, versioned public daily catalogs shared by host integrations.

Catalog validation checks declarations, not source authentication or execution
permission. Storage digests, wire digests and raw byte SHA have distinct scopes.
Legacy context projections retain their original exact JSON values.
"""
from copy import deepcopy
from datetime import date
import re

from ..daily_contract import public_capabilities, SUPPORTED_CURRENCIES
from ..evaluation import AVAILABILITY, EXECUTION, FIELDS
from ..expressions import OPERATOR_SEMANTICS_VERSION, MAX_WINDOW, _FUNCTION_ARITY, _ALIASES
from ..storage import digest, json_text
from .provider_field_semantics import synthetic_provider_fields

CATALOG_ID = 'daily-provider-catalog-v1'
SYNTHETIC_CONTRACT_ID = 'synthetic-daily-ohlcv-v1'
SYNTHETIC_CAPABILITY_ID = 'synthetic-daily-capabilities-v1'
MAX_CATALOG_BYTES = 256 * 1024
DECLARED_LIMITATIONS = [
    'Declaration/precheck only; source authentication and execution integration remain pending.',
    'Static current-vintage universe; supplier PIT and corporate-action completeness are not certified.',
    'Only unadjusted OHLC and share volume; no real VWAP or proxy.',
    'Engineering fixtures never establish real-source readiness.',
    'Final-period values/results and private oracle/review labels are unavailable.',
    'Legacy synthetic contexts and reuse guard are not connected by this module.',
]
SEMANTICS = ('Trailing row windows include the current observation; complete windows only. '
             'delay uses nonnegative rows; other windows use positive integer literals. '
             'rank is cross-sectional percentile rank with average ties; ts_std uses ddof=1. '
             'ts_corr uses complete paired windows; zero variance is missing. '
             'Infinite intermediates and comparisons with missing inputs remain missing. '
             'Only documented aliases may normalize; no field or window substitution.')
MISSING_POLICY = ('Input must be a complete declared calendar x asset grid; no missing-date filling. '
                  'Rolling warmup and undefined intermediate values remain missing.')
FINAL_POLICY = 'Final-test data and evaluation are unavailable to the provider.'


class CatalogError(ValueError):
    code = 'CATALOG_INVALID'
    retryable = False


def _need(condition, message):
    if not condition:
        raise CatalogError(message)


def _sha(value):
    return isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value) is not None


def _operators():
    return [{'name': name, 'arity': arity} for name, arity in sorted(_FUNCTION_ARITY.items())]


def synthetic_daily_projection(metadata, evaluation):
    """Exact legacy capability/development values; callers verify the host input."""
    return {
        'capabilities': {
            'fields': synthetic_provider_fields(metadata), 'operators': _operators(),
            'documented_aliases': deepcopy(_ALIASES),
            'operator_semantics_version': OPERATOR_SEMANTICS_VERSION,
            'maximum_window': MAX_WINDOW, 'data_kind': 'synthetic', 'semantics': SEMANTICS,
        },
        'development': {
            'train': deepcopy(evaluation['splits']['train']),
            'validation': deepcopy(evaluation['splits']['validation']),
            'min_assets': evaluation['min_assets'], 'evaluation_period': 'validation',
            'execution': deepcopy(EXECUTION), 'missing_policy': MISSING_POLICY,
            'final_test_policy': FINAL_POLICY,
        },
    }


def _envelope(family, contract_id, capability_id, projection):
    value = {'schema_version': 1, 'catalog_id': CATALOG_ID, 'family': family,
             'data_contract_id': contract_id, 'capability_id': capability_id,
             'public_projection': deepcopy(projection)}
    value['catalog_digest'] = digest(value)
    return validate_catalog(value)


def catalog_from_context(context):
    """Whitelist the actual synthetic context (or a targeted research_context).

    No dataset identity, market row, source descriptor, review, path or expected
    label is copied. The host resolver must independently verify the context.
    Extraction contexts have no dataset catalog and are explicitly unsupported.
    """
    _need(isinstance(context, dict), 'Context must be an object')
    base = context.get('research_context', context)
    _need(isinstance(base, dict) and 'capabilities' in base and 'development' in base,
          'This context has no daily data catalog')
    return _envelope('synthetic_daily', SYNTHETIC_CONTRACT_ID, SYNTHETIC_CAPABILITY_ID,
                     {key: deepcopy(base[key]) for key in ('capabilities', 'development')})


def catalog_from_daily_contract(contract):
    """Preserve D's upstream digest and add the actual full operator catalog."""
    public = public_capabilities(contract)
    return _envelope('declared_daily', public['contract_id'], public['capability_id'],
                     {'declared_capabilities': public, 'operators': _operators(),
                      'documented_aliases': deepcopy(_ALIASES)})


def _development(bounds):
    _need(isinstance(bounds, dict), 'Development declaration is missing')
    for key in ('train', 'validation'):
        item = bounds.get(key)
        _need(isinstance(item, dict) and set(item) == {'start', 'end'}, 'Unknown development range')
        _need(all(type(item[k]) is str and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', item[k])
                  for k in ('start', 'end')), 'Development dates require YYYY-MM-DD')
        try:
            left, right = [date.fromisoformat(item[k]) for k in ('start', 'end')]
        except (TypeError, ValueError):
            raise CatalogError('Invalid development dates') from None
        _need(left <= right, 'Development range is reversed')
    _need(bounds['train']['end'] < bounds['validation']['start'], 'Development ranges overlap')
    _need(type(bounds.get('min_assets')) is int and bounds['min_assets'] >= 3,
          'Evaluation requires at least three assets')
    _need(bounds.get('evaluation_period') == 'validation', 'Only validation is exposed')


def validate_catalog(catalog):
    """Closed envelope/digest/live operator consistency, never an observed proof."""
    try:
        text = json_text(catalog)
    except (ValueError, TypeError, RecursionError):
        raise CatalogError('Catalog must be finite JSON') from None
    _need(len(text.encode()) <= MAX_CATALOG_BYTES, 'Catalog exceeds its byte bound')
    _need(isinstance(catalog, dict) and set(catalog) == {
        'schema_version', 'catalog_id', 'family', 'data_contract_id', 'capability_id',
        'public_projection', 'catalog_digest'}, 'Unknown or missing catalog fields')
    value = deepcopy(catalog)
    _need(type(value['schema_version']) is int and value['schema_version'] == 1,
          'Unsupported catalog schema')
    _need(value['catalog_id'] == CATALOG_ID, 'Unsupported catalog ID')
    _need(_sha(value['catalog_digest']) and value['catalog_digest'] == digest({
        k: v for k, v in value.items() if k != 'catalog_digest'}), 'Catalog digest differs')
    p = value['public_projection']
    if value['family'] == 'synthetic_daily':
        _need(value['data_contract_id'] == SYNTHETIC_CONTRACT_ID and
              value['capability_id'] == SYNTHETIC_CAPABILITY_ID, 'Synthetic catalog identity differs')
        _need(isinstance(p, dict) and set(p) == {'capabilities', 'development'}, 'Unknown projection fields')
        # Import on invocation avoids the legacy context module import cycle.
        from .provider_context import ProviderCapabilities, ProviderDevelopment
        try:
            cap = ProviderCapabilities.model_validate(p['capabilities']).model_dump()
            dev = ProviderDevelopment.model_validate(p['development']).model_dump()
        except ValueError:
            raise CatalogError('Catalog differs from the legacy strict projection') from None
        _need(json_text(cap) == json_text(p['capabilities']) and json_text(dev) == json_text(p['development']), 'Projection was coerced')
        _need(json_text(cap['operators']) == json_text(_operators()) and cap['documented_aliases'] == _ALIASES,
              'Operator/alias catalog differs from the engine')
        _need(cap['operator_semantics_version'] == OPERATOR_SEMANTICS_VERSION and
              type(cap['maximum_window']) is int and cap['maximum_window'] == MAX_WINDOW and
              cap['semantics'] == SEMANTICS, 'Operator semantics differ')
        _need([f['name'] for f in cap['fields']] == [*FIELDS, 'returns'], 'Field catalog differs')
        _need(all(f['kind'] == 'raw' and f['available_at'] == AVAILABILITY[f['name']] for f in cap['fields'] if f['name'] in FIELDS) and
              cap['fields'][-1]['kind'] == 'derived' and cap['fields'][-1]['unit'] == 'dimensionless' and
              cap['fields'][-1]['available_at'] == 'after session close', 'Field availability or role differs')
        _need(json_text(dev['execution']) == json_text(EXECUTION) and dev['missing_policy'] == MISSING_POLICY and
              dev['final_test_policy'] == FINAL_POLICY, 'Execution or missing policy differs')
        _development(dev)
    elif value['family'] == 'declared_daily':
        _need(value['data_contract_id'] == 'real-daily-ohlcv-v1' and
              value['capability_id'] == 'real-daily-capabilities-v1', 'Declared catalog identity differs')
        _need(isinstance(p, dict) and set(p) == {'declared_capabilities', 'operators', 'documented_aliases'},
              'Unknown declared projection fields')
        _need(json_text(p['operators']) == json_text(_operators()) and p['documented_aliases'] == _ALIASES,
              'Operator/alias catalog differs from the engine')
        cap = p['declared_capabilities']
        _need(isinstance(cap, dict) and set(cap) == {
            'schema_version', 'capability_id', 'contract_id', 'contract_digest', 'data_kind', 'origin',
            'operator_semantics_version', 'maximum_window', 'fields', 'window_unit', 'missing_policy',
            'alpha101_epsilon', 'development', 'execution', 'limitations', 'capability_digest'},
              'Unknown declared capability fields')
        _need(type(cap['schema_version']) is int and cap['schema_version'] == 1 and
              cap['contract_id'] == value['data_contract_id'] and cap['capability_id'] == value['capability_id'],
              'Declared capability identity differs')
        _need(_sha(cap['contract_digest']) and _sha(cap['capability_digest']) and
              cap['capability_digest'] == digest({k: v for k, v in cap.items() if k != 'capability_digest'}),
              'Upstream capability digest differs')
        _need(cap['data_kind'] == 'real_daily_ohlcv' and type(cap['origin']) is str and cap['origin'] in {
            'engineering_fixture', 'real_source_declaration'}, 'Unknown data declaration')
        _need(cap['operator_semantics_version'] == OPERATOR_SEMANTICS_VERSION and
              type(cap['maximum_window']) is int and cap['maximum_window'] == MAX_WINDOW,
              'Declared operator semantics differ')
        _need(isinstance(cap['fields'], list) and all(isinstance(f, dict) for f in cap['fields']) and [f.get('name') for f in cap['fields']] == [*FIELDS, 'returns'],
              'Declared field catalog differs')
        price_currencies = [f.get('currency') for f in cap['fields'] if f['name'] in FIELDS and f['name'] != 'volume']
        _need(all(isinstance(currency, str) and currency in SUPPORTED_CURRENCIES for currency in price_currencies) and len(set(price_currencies)) == 1,
              'Declared currency differs')
        _need(next(f for f in cap['fields'] if f['name'] == 'volume').get('currency') is None, 'Volume currency must be absent')
        for f in cap['fields']:
            expected_keys = {'name', 'kind', 'unit', 'currency', 'available_at'} | (
                {'definition'} if f['name'] == 'returns' else {'adjustment'})
            _need(isinstance(f, dict) and set(f) == expected_keys, 'Unknown declared field keys')
            if f['name'] in FIELDS:
                _need(f['kind'] == 'raw' and f['available_at'] == AVAILABILITY[f['name']] and
                      f['adjustment'] == 'raw_unadjusted' and f['unit'] == (
                          'shares' if f['name'] == 'volume' else 'currency_major_unit'), 'Declared field semantics differ')
            else:
                _need(f['kind'] == 'derived' and f['unit'] == 'dimensionless' and f['currency'] is None and
                      f['available_at'] == 'after session close' and f['definition'] ==
                      'close[t]/close[t-1]-1; declared sessions; no fill; first row missing', 'Derived field differs')
        _need(cap['window_unit'] == 'declared_calendar_sessions' and
              cap['missing_policy'] == 'reject_incomplete_or_nonpositive_grid' and
              cap['alpha101_epsilon'] == {'value': .001, 'unit': 'currency_major_unit'} and
              json_text(cap['execution']) == json_text(EXECUTION), 'Declared timing or units differ')
        _need(isinstance(cap['development'], dict) and set(cap['development']) == {'train','validation','min_assets','evaluation_period'},
              'Unknown declared development fields')
        _need(cap['limitations'] == DECLARED_LIMITATIONS, 'Declared limitations differ from the public allowlist')
        _development(cap['development'])
    else:
        raise CatalogError('Unsupported catalog family')
    return value


def loop_capability_identity(catalog, dataset_id, dataset_digest):
    """Mechanically map the full public catalog to N1; does not issue a permit."""
    from .provider_loop_authority_schema import CapabilityIdentity
    c = validate_catalog(catalog)
    key = 'capabilities' if c['family'] == 'synthetic_daily' else 'declared_capabilities'
    cap = c['public_projection'][key]
    return CapabilityIdentity.model_validate({
        'data_contract_id': c['data_contract_id'], 'capability_id': c['capability_id'],
        'catalog_digest': c['catalog_digest'], 'dataset_id': dataset_id,
        'dataset_digest': dataset_digest, 'operator_semantics_id': cap['operator_semantics_version'],
        'fields': [f['name'] for f in cap['fields']],
    }).model_dump()
