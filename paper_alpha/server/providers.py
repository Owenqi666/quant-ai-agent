"""Reserved proposal boundary; no AI implementation or network behavior."""
from typing import Protocol


class ProposalProvider(Protocol):
    """Return a draft only. The service must validate it and save a new revision.

    Inputs are versioned extracted paper evidence and a data/operator catalog.
    Outputs must match the path-free task contract. Implementations may not queue
    experiments, write computed metrics, or approve human review/regression cases.
    """

    def propose(self, *, paper: dict, catalog: dict) -> dict: ...


class DisabledProposalProvider:
    enabled = False

    def propose(self, *, paper: dict, catalog: dict) -> dict:
        raise NotImplementedError("AI proposal generation is not configured. Create a reviewed manual revision.")
