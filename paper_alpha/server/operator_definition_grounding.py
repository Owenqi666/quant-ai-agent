"""Bounded selected-source operator declarations, separate from semantic review."""
from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path
import re

from ..expressions import _ALIASES, _FUNCTION_ARITY, MAX_EXPRESSION_LENGTH, MAX_AST_NODES
from ..storage import digest, json_text
from .paper_material_spans import build_span_catalog, validate_span_catalog
from .paper_segments import validate_material_draft, MaterialError

POLICY_VERSION = 'selected_operator_definitions_v1'
POLICY = {'schema_version': 1, 'policy_version': POLICY_VERSION,
          'source_scope': 'exact selected input chunks and verified span catalog only',
          'definitions': 'formal function declaration plus recognized complete meaning; not name occurrence',
          'coverage': 'complete non-whitespace declaration range in calculation and inferred-direction bindings',
          'formula_support': 'unique explicit named Alpha reference; complete bound source expression; AST comparison with documented aliases only',
          'aliases': dict(_ALIASES), 'execution_validation': 'existing engine; unsupported calls remain unsupported',
          'repair': 'append exact existing spans and binding references only; preserve formula/data/scientific unknowns',
          'semantic_fidelity': 'unverified', 'human_approval': False}
_ROOT = Path(__file__).resolve().parents[2]
_HEADER = re.compile(r'\b([A-Za-z_]\w*)\(\s*([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*){0,2})\s*\)\s*=(?!=)')
_STANDARD = re.compile(r'((?:[A-Za-z_]\w*\(\s*[A-Za-z_]\w*\)\s*,\s*)*[A-Za-z_]\w*\(\s*[A-Za-z_]\w*\))\s*=\s*standard definitions\b', re.I)


def check_policy():
    path = _ROOT / 'contracts/research_operator_definitions_v1/policy.json'
    if path.is_symlink() or path.read_text() != json_text(POLICY) + '\n':
        raise MaterialError('Operator-definition policy resource differs', code='OPERATOR_DEFINITION_POLICY_INVALID')


def _expression(hypothesis):
    text = hypothesis.get('calculation')
    if not isinstance(text, str) or len(text) > MAX_EXPRESSION_LENGTH * 2:
        return None
    # An explicit formula label and trailing explanation are read, not rewritten.
    part = text.split(';', 1)[0].strip()
    part = re.sub(r'^Alpha\s*#\s*\d+\s*(?:is|=|:)\s*', '', part, flags=re.I)
    try:
        tree = ast.parse(part, mode='eval')
    except (SyntaxError, ValueError, RecursionError):
        return None
    if isinstance(tree.body, ast.Name) and part != tree.body.id:
        return None
    if len(list(ast.walk(tree))) > MAX_AST_NODES:
        return None
    return part, tree


def _meaning(name, parameters):
    p = [re.escape(item) for item in parameters]
    if name == 'rank' and len(p) == 1:
        return r'cross[- ]sectional rank\b'
    if name == 'ts_rank' and len(p) == 2:
        return rf'time[- ]series rank (?:of {p[0]} )?(?:in|over|for) (?:the )?(?:past |last )?{p[1]} (?:days|observations)\b'
    if name in {'ts_delta', 'delta'} and len(p) == 2:
        return rf"(?:today['’]s|current) value of {p[0]} minus (?:the )?value of {p[0]} {p[1]} (?:days|observations) ago\b"
    if name == 'delay' and len(p) == 2:
        return rf'value of {p[0]} {p[1]} (?:days|observations) ago\b'
    if name in {'ts_corr', 'correlation', 'covariance'} and len(p) == 3:
        kind = 'covariance' if name == 'covariance' else 'correlation'
        return rf'(?:time[- ](?:serial|series)|rolling) {kind} (?:of|between) {p[0]} (?:and|,) {p[1]} (?:for|over) (?:the )?(?:past |last )?{p[2]} (?:days|observations)\b'
    if name in {'ts_min', 'ts_max', 'ts_sum', 'ts_mean', 'ts_std', 'stddev'} and len(p) == 2:
        terms = {'ts_min': 'min(?:imum)?', 'ts_max': 'max(?:imum)?', 'ts_sum': 'sum', 'ts_mean': '(?:mean|average)',
                 'ts_std': 'standard deviation', 'stddev': 'standard deviation'}
        return rf'(?:time[- ]series |moving time[- ]series |moving |rolling ){terms[name]} (?:of {p[0]} )?(?:over|for) (?:the )?(?:past |last )?{p[1]} (?:days|observations)\b'
    return None


def _definitions(selection, catalog):
    result = []
    for chunk in selection['chunks']:
        text = chunk['text']
        for match in _STANDARD.finditer(text):
            for name, parameter in re.findall(r'([A-Za-z_]\w*)\(\s*([A-Za-z_]\w*)\)', match[1]):
                if name.lower() in {'abs', 'log', 'sign'}:
                    result.append(_definition(chunk, match.start(), match.end(), name.lower(), catalog))
        for match in _HEADER.finditer(text):
            name = match[1].lower(); parameters = [p.strip() for p in match[2].split(',')]
            pattern = _meaning(name, parameters)
            if pattern:
                rhs = re.match(pattern, text[match.end():].lstrip(), re.I)
                if rhs:
                    start = match.start()
                    end = match.end() + len(text[match.end():]) - len(text[match.end():].lstrip()) + rhs.end()
                    result.append(_definition(chunk, start, end, _ALIASES.get(name, name), catalog))
    return result


def _definition(chunk, start, end, name, catalog):
    lo, hi = chunk['start'] + start, chunk['start'] + end
    spans = [s['id'] for s in catalog['spans'] if s['chunk_id'] == chunk['id']
             and s['normalized_start'] < hi and s['normalized_end'] > lo]
    return {'canonical_name': name, 'page': chunk['page'], 'chunk_id': chunk['id'],
            'normalized_start': lo, 'normalized_end': hi, 'declaration': chunk['text'][start:end], 'span_ids': spans}


def _covered(definition, evidence_ids, evidence, chunks):
    intervals = [(evidence[i]['normalized_start'], evidence[i]['normalized_end']) for i in evidence_ids
                 if i in evidence and evidence[i]['chunk_id'] == definition['chunk_id']]
    chunk = chunks[definition['chunk_id']]
    return all(chunk['text'][position - chunk['start']].isspace()
               or any(lo <= position < hi for lo, hi in intervals)
               for position in range(definition['normalized_start'], definition['normalized_end']))


def _canonical_ast(tree):
    class Aliases(ast.NodeTransformer):
        def visit_Call(self, node):
            self.generic_visit(node)
            if isinstance(node.func, ast.Name):
                node.func.id = _ALIASES.get(node.func.id, node.func.id)
            return node
    return ast.dump(Aliases().visit(deepcopy(tree)), include_attributes=False)


def _formula_support(hypothesis, expression, tree, bindings, evidence, chunks):
    ids = bindings.get('calculation', {}).get('evidence_ids', [])
    labels = set(re.findall(r'Alpha\s*#\s*(\d+)\b', hypothesis['claim'] + ' ' + hypothesis['calculation'], re.I))
    result = {'status': 'unresolved', 'expression': expression, 'source_label': None,
              'source_expression': None, 'cited_evidence_ids': ids, 'reason': 'SOURCE_FORMULA_UNRESOLVED'}
    if len(labels) != 1:
        return result
    label = next(iter(labels)); result['source_label'] = 'Alpha#' + label
    choices = []
    for chunk in chunks.values():
        for header in re.finditer(r'Alpha\s*#\s*' + re.escape(label) + r'\s*:\s*', chunk['text'], re.I):
            start = header.end()
            if start >= len(chunk['text']):
                continue
            end = None
            if chunk['text'][start] == '(':
                depth = 0
                for index in range(start, min(len(chunk['text']), start + MAX_EXPRESSION_LENGTH)):
                    char = chunk['text'][index]
                    if char == '(':
                        depth += 1
                    elif char == ')':
                        depth -= 1
                        if depth == 0:
                            end = index + 1; break
            else:
                # A complete named call expression ends at another explicit
                # formula/declaration marker, not at a guessed call occurrence.
                tail = chunk['text'][start:start + MAX_EXPRESSION_LENGTH]
                markers = [m.start() for m in re.finditer(r'Alpha\s*#\s*\d+\s*:|;|\.(?=\s|$)', tail, re.I)]
                markers.extend(m.start() for m in _HEADER.finditer(tail))
                stop = min(markers) if markers else len(tail)
                candidate = tail[:stop].rstrip()
                if candidate:
                    end = start + len(candidate)
            if end is None:
                continue
            source_expression = chunk['text'][start:end]
            try:
                source_tree = ast.parse(source_expression, mode='eval')
            except (SyntaxError, ValueError, RecursionError):
                continue
            span = {'chunk_id': chunk['id'], 'normalized_start': chunk['start'] + header.start(),
                    'normalized_end': chunk['start'] + end}
            if _covered(span, ids, evidence, chunks):
                choices.append((source_expression, source_tree, span))
    if len(choices) != 1:
        return result
    source_expression, source_tree, span = choices[0]
    match = _canonical_ast(tree) == _canonical_ast(source_tree)
    return {**result, 'status': 'matches' if match else 'mismatch', 'source_expression': source_expression,
            'source_formula_range': span, 'reason': None if match else 'SOURCE_FORMULA_MISMATCH'}


def assess_operator_definitions(selection, material, catalog=None):
    """Read complete declarations and their bound coverage; never execute an expression."""
    check_policy()
    catalog = validate_span_catalog(selection, catalog or build_span_catalog(selection))
    raw = material.model_dump() if hasattr(material, 'model_dump') else material
    value = validate_material_draft(selection, raw, accepted=True)['material']
    result = {'policy_version': POLICY_VERSION, 'status': 'not_applicable',
              'selection_digest': selection['selection_digest'], 'catalog_digest': catalog['catalog_digest'],
              'material_digest': digest(value), 'hypotheses': [], 'blockers': [],
              'semantic_fidelity': 'unverified', 'human_approval': False}
    if value.get('grounding_version') != 1:
        return result
    definitions = _definitions(selection, catalog)
    evidence = {e['id']: e for e in value['evidence']}; chunks = {c['id']: c for c in selection['chunks']}
    found_calls = False; parsed_calculation = False; needs_information = False; missing_coverage = False; formula_gap = False
    for hypothesis in value['hypotheses']:
        parsed = _expression(hypothesis)
        if parsed is None:
            if hypothesis.get('calculation') is not None:
                formula_gap = True
                result['blockers'].append({'code': 'SOURCE_FORMULA_UNRESOLVED', 'hypothesis_id': hypothesis['id'],
                                           'source_label': None})
            continue
        parsed_calculation = True
        expression, tree = parsed
        bindings = {b['component']: b for b in value['support_bindings'] if b['hypothesis_id'] == hypothesis['id']}
        components = ['calculation']
        if bindings.get('signal_direction', {}).get('status') == 'inference':
            components.append('signal_direction')
        formula = _formula_support(hypothesis, expression, tree, bindings, evidence, chunks)
        row = {'hypothesis_id': hypothesis['id'], 'expression': expression, 'operators': [], 'missing_operators': [],
               'formula_support': formula}
        seen = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            found_calls = True; source_name = node.func.id; name = _ALIASES.get(source_name, source_name)
            key = source_name, ast.get_source_segment(expression, node)
            if key in seen:
                continue
            seen.add(key)
            choices = [d for d in definitions if d['canonical_name'] == name]
            definition = choices[0] if choices else None
            covered_by = {component: bool(definition) and _covered(definition,
                bindings.get(component, {}).get('evidence_ids', []), evidence, chunks) for component in components}
            supported = name in _FUNCTION_ARITY
            item = {'source_name': source_name, 'canonical_name': name, 'call': key[1],
                    'execution_supported': supported, 'registry_arity': _FUNCTION_ARITY.get(name),
                    'actual_arity': len(node.args), 'definition': definition,
                    'definition_span_ids': definition['span_ids'] if definition else [],
                    'cited_evidence_ids': bindings.get('calculation', {}).get('evidence_ids', []),
                    'covered_components': covered_by, 'covered': all(covered_by.values()),
                    'reason': None if all(covered_by.values()) else 'OPERATOR_DEFINITION_MISSING'}
            row['operators'].append(item)
            if not item['covered']:
                row['missing_operators'].append(source_name)
                missing_coverage = True
                if not definition:
                    needs_information = True
                result['blockers'].append({'code': 'OPERATOR_DEFINITION_MISSING', 'hypothesis_id': hypothesis['id'],
                    'operator': source_name, 'components': [c for c, ok in covered_by.items() if not ok],
                    'definition_available_in_selected_input': bool(definition)})
        result['hypotheses'].append(row)
        if bindings.get('calculation', {}).get('status') == 'direct' and formula['status'] != 'matches':
            formula_gap = True
            result['blockers'].append({'code': formula['reason'], 'hypothesis_id': hypothesis['id'],
                                       'source_label': formula['source_label']})
    if found_calls or parsed_calculation:
        result['definition_status'] = 'needs_information' if needs_information else ('repairable' if missing_coverage else 'grounded')
        result['status'] = 'needs_information' if formula_gap else result['definition_status']
    elif formula_gap:
        result['status'] = 'needs_information'
    return result


def plan_operator_definition_repair(selection, material, catalog=None):
    """Append already supplied spans/references; preserve every original scientific field."""
    catalog = validate_span_catalog(selection, catalog or build_span_catalog(selection))
    before = assess_operator_definitions(selection, material, catalog)
    original = material.model_dump() if hasattr(material, 'model_dump') else material
    value = deepcopy(original)
    added = []
    if before['status'] == 'repairable':
        spans = {s['id']: s for s in catalog['spans']}
        for row in before['hypotheses']:
            hypothesis = next(h for h in value['hypotheses'] if h['id'] == row['hypothesis_id'])
            for operator in row['operators']:
                for component, covered in operator['covered_components'].items():
                    if covered:
                        continue
                    binding = next(b for b in value['support_bindings'] if b['hypothesis_id'] == hypothesis['id'] and b['component'] == component)
                    for span_id in operator['definition_span_ids']:
                        span = spans[span_id]
                        existing = next((e for e in value['evidence'] if all(e[k] == span[k] for k in
                            ('page', 'chunk_id', 'quote', 'normalized_start', 'normalized_end'))), None)
                        if existing is None:
                            identity = 'operator_definition_' + digest(span_id)[:24]
                            if any(e['id'] == identity for e in value['evidence']):
                                raise MaterialError('Definition evidence ID conflicts', code='DEFINITION_REPAIR_INVALID')
                            existing = {'id': identity, **{k: span[k] for k in ('page', 'chunk_id', 'quote', 'normalized_start', 'normalized_end')}, 'printed_page': None}
                            value['evidence'].append(existing); added.append(span_id)
                        if existing['id'] not in hypothesis['evidence_ids']:
                            hypothesis['evidence_ids'].append(existing['id'])
                        if existing['id'] not in binding['evidence_ids']:
                            binding['evidence_ids'].append(existing['id'])
    after = assess_operator_definitions(selection, value, catalog)
    changed = value != original
    record = {'policy_version': POLICY_VERSION, 'catalog_digest': catalog['catalog_digest'],
              'selection_digest': selection['selection_digest'], 'before_material_digest': digest(original),
              'after_material_digest': digest(value), 'added_span_ids': added,
              'status_before': before['status'], 'status_after': after['status'],
              'actor': 'automation', 'human_approval': False, 'semantic_fidelity': 'unverified'}
    return {'material': value, 'changed': changed, 'before': before, 'after': after, 'repair': record}
