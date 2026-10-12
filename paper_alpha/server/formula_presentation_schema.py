"""Presentation-only mathematical reading facts for one exact frozen target."""
from typing import Literal
from pydantic import Field, model_validator
from ..author_panel_schema import AuthorModel
from .evidence_lineage_schema import LineageTarget

SHA = r'^[0-9a-f]{64}$'

class FormulaReadingItem(AuthorModel):
    role: Literal['paper_reference', 'declared', 'executed']
    status: Literal['available', 'unsupported', 'missing']
    expression: str | None = Field(max_length=4096)
    latex: str | None = Field(max_length=65536)

    @model_validator(mode='after')
    def check_status(self):
        if self.status == 'available' and (not self.expression or not self.latex):
            raise ValueError('Available presentation requires a literal expression and derived LaTeX')
        if self.status != 'available' and self.latex is not None:
            raise ValueError('Unsupported or missing presentations cannot carry LaTeX')
        if (self.status == 'missing') != (self.expression is None):
            raise ValueError('Missing expression must remain explicitly absent')
        return self

class FormulaPresentationResponse(AuthorModel):
    schema_version: Literal[1]
    kind: Literal['run_formula_presentation']
    target: LineageTarget
    policy_version: Literal['bounded-local-dsl-latex-v1']
    items: list[FormulaReadingItem] = Field(min_length=3, max_length=3)
    alignment_public_view_digest: str = Field(pattern=SHA)
    public_projection_redacted: bool
    public_view_digest: str = Field(pattern=SHA)
    semantic_fidelity: Literal['unverified']
    human_review_written: Literal[False]
    llm_api_called: Literal[False]
    factor_computation_called: Literal[False]

    @model_validator(mode='after')
    def check_roles(self):
        if [item.role for item in self.items] != ['paper_reference', 'declared', 'executed']:
            raise ValueError('Presentation must retain exactly the three source roles')
        return self
