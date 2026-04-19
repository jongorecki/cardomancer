# web_enrichment/
# ---------------------------------------------------------------------------
# External-data enrichment package. One submodule per external source;
# every concrete source implements the EnrichmentSource contract defined
# in base.py. Consumers read via EnrichmentRepo (repo.py).
#
# Phase 0A ships the base interfaces, the refresh scheduler, and stub
# entries for every planned source. Concrete source implementations land
# in Phase 1+.
# ---------------------------------------------------------------------------

from web_enrichment.base import (
    EnrichmentSource,
    RefreshResult,
)
from web_enrichment.repo import (
    CardEnrichment,
    EnrichmentRepo,
)
from web_enrichment.scheduler import RefreshScheduler

__all__ = [
    "EnrichmentSource",
    "RefreshResult",
    "CardEnrichment",
    "EnrichmentRepo",
    "RefreshScheduler",
]
