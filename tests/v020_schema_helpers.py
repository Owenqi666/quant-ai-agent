"""New additive resources removed when reconstructing actual older schemas."""
from paper_alpha.server.claim_reviews import SCHEMA as CLAIM_REVIEW_SCHEMA
from paper_alpha.server.semantic_evaluation_sets import SCHEMA as SEMANTIC_SET_SCHEMA
from paper_alpha.server.research_bindings import SCHEMA as BINDING_SCHEMA
from paper_alpha.server.industry_mom_storage_schema import SCHEMA as INDUSTRY_MOM_SCHEMA

# Reconstructing an older marker requires removing every later table, including
# new versions. Production CREATE TABLE remains strict; fixtures must be exact.
SCHEMA = CLAIM_REVIEW_SCHEMA + SEMANTIC_SET_SCHEMA + BINDING_SCHEMA + INDUSTRY_MOM_SCHEMA
