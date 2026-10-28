"""artmig — Artifactory -> GitHub migration toolkit.

Pipeline phases (same for every artifact type): inventory -> download ->
transform -> publish -> verify. Every phase is idempotent/resumable and
supports a global --dry-run.
"""

__version__ = "0.1.0"
