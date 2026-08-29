"""Exact-partition authorization for the embedded runtime."""

from __future__ import annotations

from collections.abc import Iterable

from recall_origin.contracts.errors import SCOPE_DENIED, RecallOriginError
from recall_origin.contracts.v1 import PartitionRef, PrincipalContext
from recall_origin.domain.enums import Capability


class AuthorizationPolicy:
    """Authorize capabilities against an explicit set of exact partitions.

    ``OriginContext`` fields are deliberately absent here.  A session or host
    agent identifies where a write came from; it does not grant read access.
    """

    def __init__(
        self,
        principal: PrincipalContext,
        allowed_partitions: Iterable[PartitionRef] | None = None,
    ) -> None:
        self.principal = principal
        self._allowed = frozenset(partition.serialize() for partition in (allowed_partitions or ()))

    def allows_partition(self, partition: PartitionRef) -> bool:
        """Return whether the trusted startup scope includes this exact partition."""

        if self.principal.auto_grant_local_scopes and self.principal.tenant_id == "local":
            return True
        return partition.serialize() in self._allowed

    def allows(self, capability: Capability, partition: PartitionRef) -> bool:
        if capability not in self.principal.capabilities:
            return False
        return self.allows_partition(partition)

    def require(self, capability: Capability, partition: PartitionRef) -> None:
        if not self.allows(capability, partition):
            raise RecallOriginError(
                SCOPE_DENIED,
                "The principal is not authorized for the requested scope.",
                details={
                    "capability": capability.value,
                    "scope": partition.serialize(),
                },
            )
