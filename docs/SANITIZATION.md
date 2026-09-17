# Public package sanitization

An allowlist-based copy was made from the recovered project. Original working directories were not used as public repositories and their Git history was not copied.

Excluded: `.env`, credentials, databases, logs, private input/output directories, client XML/CSV, workbooks, stakeholder HTML, existing screenshots, research snapshots, caches, browser profiles, delivery archives, deployment configuration and upstream binaries. Existing fixtures derived from a real portfolio by scaling amounts were also excluded.

Only independently synthetic examples created for this release are used for portfolio reporting. The MAE package includes no copied research observations or predictions. Two inspected non-client JSON configuration files are retained so imports can work.

Checks: scan packaged text for key/token formats, credential assignments, personal paths and email addresses; compare against non-empty secret values from the original environment without printing those values; inspect the final ZIP inventory and hashes. The generic `/Users/` string in a validator is intentional defensive code, not a personal path.

This is an evidence-based cleanup of the packaged files, not a claim that the entire original project/history was audited. No deployment or credentials are shipped.
