"""Domain-specific exceptions raised by DWPDSim."""


class DWPDSimError(Exception):
    """Base class for simulator errors."""


class BlockNotFoundError(DWPDSimError):
    """Raised when a query references a block absent from storage."""


class StorageCapacityError(DWPDSimError):
    """Raised when authoritative storage cannot hold another block."""


class InvalidPolicyDecisionError(DWPDSimError):
    """Raised when a policy returns a decision the manager cannot execute."""


class OutOfOrderQueryError(DWPDSimError):
    """Raised when query timestamps decrease during a simulation."""
