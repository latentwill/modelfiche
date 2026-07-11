"""Storage-domain contracts, persistence helpers, and safety primitives."""

from .identity import SourceIdentity, SourceIdentityError, normalize_source_identity
from .redaction import redact_for_persistence
from .cache import ThumbnailCache, ThumbnailCapacityError, ThumbnailGrant
from .capacity import CapacityExceeded, CapacityLedger, CapacityReservation, ReservationReapRequired
from .leases import LeaseClaim, LeaseFenceError, LeaseRegistry, SupervisorLock
from .local_root import LocalRoot, LocalRootSafetyError, RootFingerprint
from .publication import LocalPublisher, PublicationReceipt

__all__ = [
    "SourceIdentity",
    "SourceIdentityError",
    "normalize_source_identity",
    "redact_for_persistence",
    "ThumbnailCache",
    "ThumbnailCapacityError",
    "ThumbnailGrant",
    "CapacityExceeded",
    "CapacityLedger",
    "CapacityReservation",
    "ReservationReapRequired",
    "LeaseClaim",
    "LeaseFenceError",
    "LeaseRegistry",
    "SupervisorLock",
    "LocalRoot",
    "LocalRootSafetyError",
    "RootFingerprint",
    "LocalPublisher",
    "PublicationReceipt",
]
