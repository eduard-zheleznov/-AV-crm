# Deployment baseline

The target is a dedicated Windows remote computer with Python and Chromium.
There is no production deployment yet. The feature branch will provide an
installer and a staged dry-run procedure before live CRM writes are enabled.

Rollback is performed by stopping the worker and checking out the previous Git
tag/commit. Runtime state is persistent and is not overwritten by code updates.

