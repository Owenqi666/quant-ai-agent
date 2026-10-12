"""Trusted host routing; Native receives a concrete leaf, never this router."""
from contextlib import closing
from .db import connect
from .provider_host_authority import HostResearchAuthority, HostAuthorityError
from .provider_loop_native_authority import LoopNativeResearchAuthority


class ResearchAuthorityRouter:
    def __init__(self, store, registry):
        self.store = store
        self.host = HostResearchAuthority(store, registry)
        self.loop = LoopNativeResearchAuthority(store, registry)

    def leaf_for_session(self, session_id, *, connection=None):
        if connection is None:
            with closing(connect(self.store.db_path)) as opened:
                return self.leaf_for_session(session_id, connection=opened)
        leaves = [leaf for leaf in (self.host, self.loop) if leaf.owns_session(connection, session_id)]
        if len(leaves) > 1:
            raise HostAuthorityError('Session has conflicting trusted authorities', 'RESEARCH_AUTHORITY_CONFLICT')
        return leaves[0] if leaves else None

    def owns_session(self, connection, identity):
        return self.leaf_for_session(identity, connection=connection) is not None

    def _leaf(self, connection, request):
        if hasattr(request, 'model_dump'):
            request = request.model_dump()
        leaf = self.leaf_for_session(request['session_id'], connection=connection)
        if leaf is None:
            raise HostAuthorityError('Session has no trusted authority', 'RESEARCH_AUTHORITY_REQUIRED')
        return leaf

    def verify_request(self, connection, request, config, *, phase):
        return self._leaf(connection, request).verify_request(connection, request, config, phase=phase)

    def reserve_session_action(self, connection, request, reservation):
        leaf = self.leaf_for_session(request['session_id'], connection=connection)
        if type(leaf) is LoopNativeResearchAuthority:
            return leaf.reserve_session_action(connection, request, reservation)
        # Unbound fixtures and original Host sessions keep their original ledger.
        return None

    def reserve_attempt(self, connection, proposal, attempt_id):
        return self._leaf(connection, proposal['request']).reserve_attempt(connection, proposal, attempt_id)

    def authorize_prepared(self, proposal, attempt, prepared_digest):
        leaf = self.leaf_for_session(proposal['request']['session_id'])
        if leaf is None:
            raise HostAuthorityError('Prepared request has no trusted authority', 'RESEARCH_AUTHORITY_REQUIRED')
        return leaf.authorize_prepared(proposal, attempt, prepared_digest)

    def verify_result(self, proposal, attempt, *, response, receipt, error):
        leaf = self.leaf_for_session(proposal['request']['session_id'])
        if leaf is None:
            raise HostAuthorityError('Result has no trusted authority', 'RESEARCH_AUTHORITY_REQUIRED')
        return leaf.verify_result(proposal, attempt, response=response, receipt=receipt, error=error)

    def observe_transport(self, proposal, attempt, *, receipt, error, connection=None):
        leaf = self.leaf_for_session(proposal['request']['session_id'], connection=connection)
        if type(leaf) is LoopNativeResearchAuthority:
            return leaf.observe_transport(proposal, attempt, receipt=receipt, error=error, connection=connection)
        return None

    def cancelled(self, proposal):
        leaf = self.leaf_for_session(proposal['request']['session_id'])
        return leaf.cancelled(proposal) if leaf is not None else False

    # These compatibility methods retain the old two-operation Host contract.
    def create_session(self, body, source, config):
        return self.host.create_session(body, source, config)

    def inspect_source(self, *args, **kwargs):
        return self.host.inspect_source(*args, **kwargs)

    def verify_session_binding(self, *args, **kwargs):
        return self.host.verify_session_binding(*args, **kwargs)

    def capability(self, *args, **kwargs):
        return self.host.capability(*args, **kwargs)

    def capabilities(self):
        return self.host.capabilities()
