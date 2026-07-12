# Archived scripts

This directory contains one-time migrations, historical repair jobs, deployment
helpers, and exploratory experiments that are no longer part of the supported
runtime paths.

Archived scripts are retained for auditability and historical recovery. They
are not maintained, scheduled, or included in the normal test suite.

Keep reusable behavior in `crawler/` or `utils/`, and expose recurring
operations through a small CLI under `scripts/`. New one-off work belongs here
from the start, with a short note explaining its input and completion status.

Before re-running an archived data mutation, inspect its assumptions against
the current schema and use a dry-run or database backup where available.

The supported entry points are documented in the repository README. Moving a
script here does not remove its history; use `git log --follow` to inspect its
original implementation.
