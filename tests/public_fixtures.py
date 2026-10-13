"""Small original paper/task fixtures using the real Store and synthetic dataset."""
from io import BytesIO

from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject


QUOTE = "((close - open) / ((high - low) + 0.001))"
CANDIDATE_ID = 'toy-c'


def original_pdf():
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = writer._add_object(DictionaryObject({
        NameObject('/Type'): NameObject('/Font'),
        NameObject('/Subtype'): NameObject('/Type1'),
        NameObject('/BaseFont'): NameObject('/Helvetica')}))
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):
        DictionaryObject({NameObject('/F1'): font})})
    stream = DecodedStreamObject()
    text = 'Original test definition: signal = ' + QUOTE + '.'
    literal = text.replace('(', r'\(').replace(')', r'\)')
    stream.set_data(f'BT /F1 12 Tf 40 740 Td ({literal}) Tj ET'.encode('ascii'))
    page[NameObject('/Contents')] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def prepare_original_research(store):
    """Register the original PDF; return its bytes and a fresh HTTP request body."""
    pdf = original_pdf()
    paper = store.add_paper(pdf, 'Original queue reliability fixture')
    dataset = store.get_dataset(store.example_dataset_id)
    dates = dataset['metadata']['calendar_dates']
    task = {
        'schema_version': 1,
        'evidence': [{'id': 'toy-quote', 'page': 1, 'quote': QUOTE}],
        'hypotheses': [{'id': 'toy-h', 'claim': 'The original fixture defines the close-open difference divided by the high-low range plus 0.001.',
            'attribution': 'paper_original', 'evidence_ids': ['toy-quote'],
            'economic_mechanism': 'No predictive economic mechanism is claimed by this software fixture.',
            'mechanism_attribution': 'user_modification',
            'signal_direction': 'Positive is long under a toy engineering convention only.',
            'required_fields': ['open', 'close', 'high', 'low'],
            'assumptions': ['Synthetic data and toy definition; no research effectiveness claim.']}],
        'candidates': [{'id': CANDIDATE_ID, 'hypothesis_id': 'toy-h', 'expression': QUOTE,
                        'origin': 'paper_original', 'changes': []}],
        'evaluation': {'min_assets': 3, 'splits': {
            'train': {'start': dates[0], 'end': dates[9]},
            'validation': {'start': dates[10], 'end': dates[19]},
            'test': {'start': dates[20], 'end': dates[29]}}},
        'budget': {'max_candidates': 1, 'max_attempts_per_candidate': 1,
                   'max_tool_calls': 4, 'max_seconds': 30}}
    body = {'title': 'Original reliability research', 'paper_id': paper['id'],
            'dataset_id': dataset['id'], 'task': task,
            'idempotency_key': 'public-research-request'}
    return pdf, body


def create_original_research(store):
    """Create an ordinary unbound research; setup writes no mutation receipt."""
    _pdf, body = prepare_original_research(store)
    # Receipt tests begin with no setup receipt, matching the old seed_example.
    # Idempotent operations under test still use their original keys and paths.
    research = store.create_research(body['title'], body['paper_id'], body['dataset_id'], body['task'])
    return {'research_id': research['id'], 'revision_id': research['latest_revision_id'],
            'candidate_id': CANDIDATE_ID, 'paper_id': body['paper_id'], 'dataset_id': body['dataset_id']}
