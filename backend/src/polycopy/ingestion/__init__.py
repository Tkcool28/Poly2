"""Trade ingestion: Polymarket API client + bounded ingestion service.

Safety rules baked in here:

* All API calls go through one client with a global concurrency cap
  (``POLYCOPY_INGESTION_MAX_CONCURRENT_REQUESTS``) and bounded batch sizes
  (``POLYCOPY_INGESTION_BATCH_SIZE``). The old SQLite implosion was caused
  by unbounded concurrent pulls; that failure mode is designed out here.
* Deduplication is by the canonical composite key defined in
  ``docs/source-identity-contract.md`` — there is no trade ID field in the
  Data API, and ``outcomeIndex`` is never used as identity.
"""
