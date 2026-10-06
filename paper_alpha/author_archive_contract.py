"""Pinned public, perturbed author archive. No implicit network access."""
from copy import deepcopy

ADAPTER_VERSION = 'gjs-v2-monthly-panel-v1'
SEMANTICS_VERSION = 'gjs-v2-alignment-diagnostic-v1'
MAX_PANEL_BYTES = 768 * 1024
MAX_ROWS = 512
SOURCES = {
    'IntnlData.mat': {
        'doi': '10.7910/DVN/R1UI1J', 'version': '2.0', 'file_id': 10519941,
        'filename': 'IntnlData.mat',
        'sha256': '6fe6fc27a85ec0f305d297b95d05039cb7bd2207ce012625df1e44bc272549ed',
        'repository_md5': '87d9424ee3b5b2fce4a4fb3646cf6df6', 'bytes': 358892265,
        'assets': 59367, 'periods': 384, 'first_month': '1989-01', 'last_month': '2020-12',
    },
    'USData.mat': {
        'doi': '10.7910/DVN/R1UI1J', 'version': '2.0', 'file_id': 10519940,
        'filename': 'USData.mat',
        'sha256': '0b3f60867708c707816caa9ef856d5579c9c732af24acc734bb47d387ee4ab1d',
        'repository_md5': 'a84f4ab4be1b9a26f4822b048abad9fe', 'bytes': 93635256,
        'assets': 25437, 'periods': 1140, 'first_month': '1926-01', 'last_month': '2020-12',
    },
}
LIMITATIONS = [
    'Public author data are scrambled and randomly deleted; not unaltered market observations.',
    'Anonymous original row identifiers are not ticker or security identifiers.',
    'DGW is precomputed by the author; daily ID construction cannot be verified from this monthly panel.',
    'Formation readiness is project diagnostic availability, not all original-paper eligibility filters.',
    'No fill, future-label filtering, portfolio returns, transaction costs or paper-performance reproduction.',
    'Normalized-panel validation alone does not authenticate the claimed raw MAT source.',
]


def source_metadata(filename):
    if filename not in SOURCES:
        raise ValueError('Unsupported author source; expected pinned Dataverse V2 MAT file')
    return deepcopy(SOURCES[filename])
