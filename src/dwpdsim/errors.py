"""Domain-specific exceptions raised by DWPDSim."""


class DWPDSimError(Exception):
    """Base class for simulator errors."""


class BlockNotFoundError(DWPDSimError):
    """Raised when an operation requires a missing block."""


class StorageCapacityError(DWPDSimError):
    """Raised when persistent storage cannot complete an operation."""


class InvalidPolicyDecisionError(DWPDSimError):
    """Raised when a manager cannot execute a policy decision."""


class OutOfOrderQueryError(DWPDSimError):
    """Raised when query timestamps decrease during a simulation."""
