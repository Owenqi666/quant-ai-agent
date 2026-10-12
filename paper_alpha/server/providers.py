"""Typed proposal boundary; domain validation and execution belong to the host."""
from datetime import datetime, timezone
from typing import Protocol

from .provider_schema import (
    PreparedProposalRequest, ProviderAttemptResult, ProviderCallControl,
    ProviderError, ProviderReadiness, ProviderReceipt,
)


class ProposalProvider(Protocol):
    """Produce an untrusted draft response; never queue or approve experiments."""

    async def check_ready(self) -> ProviderReadiness: ...

    async def propose(self, request: PreparedProposalRequest,
                      control: ProviderCallControl) -> ProviderAttemptResult: ...


class DisabledProposalProvider:
    enabled = False

    async def check_ready(self) -> ProviderReadiness:
        return ProviderReadiness(provider="disabled", checked_at=datetime.now(timezone.utc).isoformat(),
                                 errors=[ProviderError(code="PROVIDER_DISABLED", message="Proposal generation is disabled.")])

    async def propose(self, request: PreparedProposalRequest,
                      control: ProviderCallControl) -> ProviderAttemptResult:
        now = datetime.now(timezone.utc).isoformat()
        return ProviderAttemptResult(None, ProviderError(code="PROVIDER_DISABLED", message="Proposal generation is disabled."),
            ProviderReceipt(proposal_id=request.proposal_id, attempt_id=request.attempt_id,
                            started_at=now, finished_at=now, elapsed_seconds=0,
                            requested_model=request.config.model, fixture=request.config.mode == "fixture"))
