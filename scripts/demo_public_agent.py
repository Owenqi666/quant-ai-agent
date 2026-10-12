"""Offline public Agent workflow: fixture replies, real parsers and Worker.

Run from a source checkout with its web/dev dependencies installed. No network
model, authentication, private paper, monkeypatch, or fabricated result is used.
The fixture protocol performs literal citation validation, not semantic review.
"""
from __future__ import annotations
import argparse
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient
from paper_alpha.server.api import create_app
from paper_alpha.server.material_context import unresolved_inventory
from paper_alpha.server.paper_materials import PaperMaterials
from paper_alpha.server.provider_contract import canonical_json
from paper_alpha.server.provider_runtime import ProviderRegistry
from paper_alpha.server.provider_schema import ProviderAttemptResult, ProviderConfig, ProviderReceipt
from paper_alpha.server.proposal_worker import ProposalWorker
from paper_alpha.server.research_session_experiments import SessionExperiments
from paper_alpha.server.runner import Worker, worker_lock
from paper_alpha.server.service import now

PREFIX = '/api/research-v2'
ASSETS = ROOT / 'examples/public_agent'
FORMULA = 'close - open'


def write(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n')


class TeachingFixtureTransport:
    """Responses authored for this original note, bound to actual request IDs."""
    def __init__(self, output):
        self.output, self.label, self.calls = Path(output), None, 0

    async def propose(self, request, control):
        self.calls += 1
        payload = json.loads(request.input_text.split('REQUEST_JSON follows:\n', 1)[1])
        context = payload['context']; operation = payload['operation']
        folder = self.output / 'proposals' / self.label
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'prompt.txt').write_text(request.input_text)
        write(folder / 'context.json', context)
        output = {'schema_version': 2, 'contract_id': 'research_provider_v2', 'operation': operation,
            'proposal_id': request.proposal_id, 'attempt_id': request.attempt_id,
            'context_digest': request.context_digest, 'outcome': 'material' if operation == 'extract_material' else 'draft',
            'material': None, 'draft': None, 'decision': None, 'abstention': None}
        if operation == 'extract_material':
            # Selection text is the only research source exposed in this request.
            assert 'evidence' not in context and 'hypotheses' not in context and 'development' not in context
            write(self.output / 'paper-selection.json', context['paper_selection'])
            phrases = {'formula': 'Formula: score = close - open.',
                       'named_formula': 'Alpha#9001: (close - open)',
                       'fields': 'Fields: open is the opening price',
                       'direction': 'Mathematical direction: a higher score',
                       'unknown': 'Scientific unknown: no economic mechanism'}
            evidence = []; links = {}
            for name, phrase in phrases.items():
                span = next(s for s in context['material_span_catalog']['spans'] if phrase in s['quote'])
                prior = next((e for e in evidence if e['span_id'] == span['id']), None)
                if prior is None:
                    prior = {'id': name, 'span_id': span['id']}; evidence.append(prior)
                links[name] = prior['id']
            output['material'] = {'title': 'Original public teaching score', 'evidence': evidence,
                'hypotheses': [{'id': 'teaching_score',
                    'claim': 'The original teaching note defines Alpha#9001, its own same-session close-minus-open score identifier.',
                    'claim_attribution': 'paper_original', 'evidence_ids': list(dict.fromkeys(links.values())),
                    'economic_mechanism': None, 'mechanism_attribution': 'unresolved',
                    'signal_direction': 'Higher scores mean larger same-session close-minus-open displacement; no future-return prediction is asserted.',
                    'required_data': [{'name': name, 'semantics': meaning, 'evidence_ids': [links['fields']]}
                        for name, meaning in [('open', 'Session t opening price in the synthetic price unit.'),
                                              ('close', 'Same asset and session t closing price in the same synthetic price unit.')]],
                    'calculation': FORMULA, 'conditions': ['Score is available after session t closes.'],
                    'unresolved_rules': ['[scientific] Economic mechanism and predictive validity remain unresolved.']}],
                'unresolved_rules': [],
                'limitations': ['Hand-authored fixture transport, original teaching source and synthetic inputs; semantic fidelity is unverified.']}
        else:
            # Candidate output must preserve the actual accepted projection.
            output['draft'] = {'evidence': deepcopy(context['evidence']), 'hypotheses': deepcopy(context['hypotheses']),
                'candidates': [{'id': 'teaching_candidate', 'hypothesis_id': 'teaching_score',
                                'expression': FORMULA, 'origin': 'paper_original', 'changes': []}]}
        if self.label == 'invalid':
            # A well-shaped reply with the wrong request binding: the production
            # parser, not this script, must reject it before material adoption.
            output['context_digest'] = '0' * 64
        raw = canonical_json(output).encode()
        (folder / 'raw-response.json').write_bytes(raw)
        return ProviderAttemptResult(raw, None, ProviderReceipt(proposal_id=request.proposal_id,
            attempt_id=request.attempt_id, started_at=now(), finished_at=now(), elapsed_seconds=0., exit_code=0,
            fixture=True, invocation_started=True, response_sha256=hashlib.sha256(raw).hexdigest()))


def run(output_dir):
    output = Path(output_dir).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError('Output directory must be new or empty; existing evidence is never overwritten.')
    output.mkdir(parents=True, exist_ok=True)
    for source, dest in [('teaching_note.pdf', 'paper.pdf'), ('spec.json', 'spec.json'),
                         ('market.csv', 'input/market.csv'), ('metadata.json', 'input/metadata.json'),
                         ('evaluation.json', 'input/evaluation.json')]:
        target = output / dest; target.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(ASSETS / source, target)
    spec = json.loads((output / 'spec.json').read_text())
    config = ProviderConfig(config_id='public-teaching-fixture', mode='fixture', enabled=True,
                            executable=sys.executable, model='hand-authored-teaching-response')
    registry = ProviderRegistry({config.config_id: config}, allow_fixture=True)
    app = create_app(output / 'workspace', provider_registry=registry)
    store = app.state.store
    proposals = registry.proposal_requests(store)
    transport = TeachingFixtureTransport(output)
    with TestClient(app) as client:
        def post(path, body=None, expected=200, **kwargs):
            response = client.post(path, json=body, **kwargs) if not kwargs else client.post(path, **kwargs)
            if response.status_code != expected:
                raise RuntimeError(f'{path}: HTTP {response.status_code}: {response.text}')
            return response.json()
        def get(path):
            response = client.get(path)
            if response.status_code != 200:
                raise RuntimeError(f'{path}: HTTP {response.status_code}: {response.text}')
            return response.json()
        def session(source, key):
            preview = post(PREFIX + '/source-preview', {'source': source, 'provider_config_id': config.config_id})
            return post(PREFIX + '/sessions', {'source': source,
                'expected_source_digest': preview['source']['source_digest'], 'provider_config_id': config.config_id,
                'expected_provider_config_digest': preview['config']['provider_config_digest'],
                'purpose': 'Original public teaching workflow; fixture transport, scientific unknown, no human approval.',
                'budget': {'max_provider_dispatches': 1, 'max_tool_actions': 8, 'max_experiments': 1,
                           'max_failures': 2, 'max_seconds': 300}, 'idempotency_key': key}, 201)
        def proposal(source, parent, label, operation):
            transport.label = label
            body = {'operation': operation, 'source': source, 'session_id': parent['id'],
                'expected_session_digest': parent['state_digest'], 'expected_source_digest': parent['current_source']['source_digest'],
                'provider_config_id': config.config_id, 'idempotency_key': 'demo-' + label,
                'instruction': spec['instruction' if operation == 'extract_material' else 'generation_instruction'],
                'budget': {'max_provider_invocations': 1, 'max_seconds': 120}}
            write(output / 'proposals' / label / 'request.json', body)
            value = post(PREFIX + '/proposals', body, 202)
            if not asyncio.run(ProposalWorker(proposals).run_once(transport)):
                raise RuntimeError('ProposalWorker did not claim the fixture request.')
            value = get(PREFIX + '/proposals/' + value['id'])
            write(output / 'proposals' / label / 'status.json', value)
            if value['accepted_response'] is not None:
                write(output / 'proposals' / label / 'accepted-response.json', value['accepted_response'])
            current = get(PREFIX + '/sessions/' + parent['id'])
            reconciled = post(PREFIX + '/sessions/' + parent['id'] + '/reconcile',
                              {'token': current['reservations'][0]['token']})
            if reconciled['reservation']['status'] != 'finished':
                raise RuntimeError('Proposal reservation was not finished.')
            return value, reconciled['session']

        paper = post('/api/papers', expected=201, files={'file': ('teaching-note.pdf', (output / 'paper.pdf').read_bytes(), 'application/pdf')},
                     data={'title': 'Original public teaching note'})
        write(output / 'paper.json', paper)
        uploaded = post('/api/dataset-imports', expected=201, files={
            'file': ('market.csv', (output / 'input/market.csv').read_bytes(), 'text/csv'),
            'metadata': ('metadata.json', (output / 'input/metadata.json').read_bytes(), 'application/json')},
            data={'title': 'Original public synthetic teaching grid', 'idempotency_key': 'demo-dataset-upload'})
        valid = post('/api/dataset-imports/' + uploaded['id'] + '/validate', {'idempotency_key': 'demo-dataset-validation'})
        if valid['status'] != 'valid':
            raise RuntimeError('Synthetic input validation failed: ' + json.dumps(valid))
        registration = post('/api/dataset-imports/' + uploaded['id'] + '/register', {
            'idempotency_key': 'demo-dataset-register', 'input_digest': valid['input_digest'],
            'validation_attempt_id': valid['latest_validation']['id'], 'report_digest': valid['latest_validation']['report_digest']})
        write(output / 'input/registration.json', registration)
        source = {'kind': 'paper_selection', 'paper_id': paper['id'], 'selected_pages': [1, 2]}
        invalid, _ = proposal(source, session(source, 'demo-invalid-session'), 'invalid', 'extract_material')
        if invalid['status'] != 'failed' or invalid['accepted_response'] is not None or invalid['error']['code'] != 'REQUEST_MISMATCH':
            raise RuntimeError('The deliberately invalid response was not rejected by the formal parser.')
        extracted, _ = proposal(source, session(source, 'demo-extract-session'), 'extract', 'extract_material')
        if extracted['status'] != 'ready':
            raise RuntimeError('Extraction fixture failed: ' + json.dumps(extracted['error']))
        imported = post(PREFIX + '/proposals/' + extracted['id'] + '/material', expected=201)
        write(output / 'material/import.json', imported)
        # Legacy fixture transport does not produce component bindings. This
        # separate, explicitly host-authored metadata adoption uses the existing
        # production material service. It preserves every original draft field,
        # keeps its raw response, and is not labelled a model grounding result.
        bound = deepcopy(imported['draft'])
        h = bound['hypotheses'][0]
        evidence = bound['evidence']
        def support(phrase):
            return [next(e['id'] for e in evidence if phrase in e['quote'])]
        component_evidence = {'claim': support('Alpha#9001: (close - open)'),
            'signal_direction': support('Mathematical direction:'),
            'calculation': support('Alpha#9001: (close - open)'),
            'condition:0': support('The score is available only after session t closes.')}
        component_evidence.update({'data:' + d['name']: d['evidence_ids'] for d in h['required_data']})
        bound.update(grounding_version=1, paper_statistics=[], support_bindings=[
            {'hypothesis_id': h['id'], 'component': component, 'evidence_ids': ids,
             'status': 'direct', 'explanation': 'Explicit host-authored teaching-source binding; literal validity is checked, semantic fidelity remains unverified.'}
            for component, ids in component_evidence.items()] + [
            {'hypothesis_id': h['id'], 'component': 'economic_mechanism', 'evidence_ids': [],
             'status': 'unresolved', 'explanation': 'The source explicitly supplies no economic mechanism or predictive validity.'}])
        component_version = PaperMaterials(store).adopt(imported['id'], imported['current_digest'], bound,
            idempotency_key='demo-host-component-bindings', actor='automation')
        write(output / 'material/component-binding-version.json', {
            'source': 'explicit_host_automation', 'host_authored_component_bindings': True,
            'is_model_output': False, 'human_approval': False, 'semantic_fidelity': 'unverified',
            'version': get(PREFIX + '/materials/' + imported['id'] + '/versions/' + component_version['id'])})
        dispositions = [{'path': path, 'original': rule, 'disposition': 'scientific_unresolved',
            'rationale': 'Keep scientific unknowns; this explicit automation configuration verifies arithmetic only.'}
            for path, rule in unresolved_inventory(imported['draft']).items()]
        adopted = post(PREFIX + '/materials/' + imported['id'] + '/engineering', {
            'expected_digest': component_version['digest'], 'idempotency_key': 'demo-engineering',
            'unresolved_dispositions': dispositions}, 201)
        write(output / 'material/engineering-version.json', adopted)
        if adopted['version']['actor'] != 'automation' or adopted['human_approval']:
            raise RuntimeError('Engineering adoption must remain explicit automation without human approval.')
        source = {'kind': 'accepted_material', 'material_id': imported['id'], 'version_id': adopted['version']['id'],
            'dataset_id': registration['registered_dataset_id'],
            'evaluation': json.loads((output / 'input/evaluation.json').read_text()),
            'method_policy': adopted['method_policy'], 'destination': {'kind': 'new_research'},
            'title': 'Public Agent demo - close minus open (fixture, synthetic)'}
        generated, parent = proposal(source, session(source, 'demo-generate-session'), 'generate', 'generate_candidates')
        if generated['status'] != 'ready':
            raise RuntimeError('Generation fixture failed: ' + json.dumps(generated['error']))
        experiment = post(PREFIX + '/experiments', {'session_id': parent['id'], 'expected_session_digest': parent['state_digest'],
            'proposal_id': generated['id'], 'accepted_response_digest': generated['accepted_response_digest'],
            'budget': {'max_steps': 5, 'max_failures': 1, 'max_seconds': 240}, 'idempotency_key': 'demo-experiment'}, 201)
        for action in ('validate', 'commit', 'submit'):
            experiment = post(PREFIX + '/experiments/' + experiment['id'] + '/advance',
                              {'action': action, 'idempotency_key': 'demo-' + action})
        with worker_lock(store.root) as descriptor:
            if not Worker(store, descriptor).run_once():
                raise RuntimeError('The original Worker did not claim the submitted run.')
        experiment = SessionExperiments(store).get(experiment['id'])
        if experiment['job']['state'] != 'completed' or experiment['reservation']['status'] != 'finished':
            raise RuntimeError('The actual job and session reservation did not complete.')
        write(output / 'experiment.json', experiment)
        run = get('/api/runs/' + experiment['job']['run_id']); write(output / 'run.json', run)
        research_id = experiment['job']['research_id']
        write(output / 'research.json', get('/api/researches/' + research_id))
        report = post('/api/researches/' + research_id + '/reports', {'run_ids': [run['id']], 'idempotency_key': 'demo-report'}, 201)
        write(output / 'report.json', report)
        downloads = {'candidates/teaching_candidate/attempt-1/factor.csv': 'factor.csv',
            'candidates/teaching_candidate/attempt-1/result.json': 'result.json',
            'inputs/task.json': 'task.json', 'report.md': 'report.md'}
        selected_artifacts = []
        for artifact in report['payload']['runs'][0]['artifacts']:
            if artifact['name'] not in downloads:
                continue
            response = client.get('/api/runs/' + run['id'] + '/artifacts/' + artifact['id'])
            if response.status_code != 200 or hashlib.sha256(response.content).hexdigest() != artifact['sha256']:
                raise RuntimeError('Normal original Worker artifact download failed its receipt.')
            target = output / 'worker' / downloads[artifact['name']]
            target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(response.content)
            selected_artifacts.append({**artifact, 'public_copy': 'worker/' + downloads[artifact['name']]})
        if len(selected_artifacts) != len(downloads):
            raise RuntimeError('Expected original Worker exports are missing.')
        write(output / 'worker/receipts.json', selected_artifacts)
        for format_, filename in [('json', 'report-export.json'), ('markdown', 'report.md')]:
            response = client.get('/api/research-reports/' + report['id'] + '/export', params={
                'format': format_, **({'view': 'readable', 'locale': 'en', 'renderer_version': 2} if format_ == 'markdown' else {})})
            if response.status_code != 200:
                raise RuntimeError('Normal report export failed.')
            (output / filename).write_bytes(response.content)
        summary = {'schema_version': 1, 'status': 'completed', 'case_id': spec['case_id'],
            'workspace': 'workspace', 'provider_mode': 'fixture', 'fixture_dispatches': transport.calls,
            'real_model_dispatches': 0, 'worker_runs': len(store.list_runs()), 'invalid_response': {
                'proposal_id': invalid['id'], 'status': invalid['status'], 'code': invalid['error']['code']},
            'paper_id': paper['id'], 'material_id': imported['id'], 'version_id': adopted['version']['id'],
            'research_id': research_id, 'run_id': run['id'], 'report_id': report['id'],
            'citation_integrity': imported['citation_integrity'], 'semantic_fidelity': 'unverified',
            'extraction_schema': spec['extraction_schema'], 'host_authored_component_bindings': True,
            'economic_mechanism': None, 'mechanism_attribution': 'unresolved', 'human_approval': False,
            'human_review': {'status': 'pending_human', 'scores': None}, 'scientific_validity': 'unknown',
            'scope': spec['scope'], 'ui': {'research_id': research_id,
                'start': 'python -m paper_alpha.server.launcher --home ' + str(output / 'workspace')},
            'files': sorted(p.relative_to(output).as_posix() for p in output.rglob('*') if p.is_file() and 'workspace' not in p.relative_to(output).parts)}
        write(output / 'demo-summary.json', summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=Path('.demo/agent'))
    args = parser.parse_args(argv)
    new_output = not args.output_dir.exists() or not any(args.output_dir.iterdir())
    try:
        result = run(args.output_dir)
    except Exception as exc:
        if new_output:
            args.output_dir.mkdir(parents=True, exist_ok=True)
            write(args.output_dir / 'failure.json', {'status': 'failed', 'error': str(exc), 'fixture_only': True,
                                                   'human_approval': False})
        raise
    print(json.dumps({key: result[key] for key in ('status', 'research_id', 'run_id', 'report_id', 'fixture_dispatches', 'real_model_dispatches')}, indent=2))


if __name__ == '__main__':
    main()
