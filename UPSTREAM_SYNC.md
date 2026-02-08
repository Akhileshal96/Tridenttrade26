# Upstream snapshot sync

This repository in the current execution environment may start as a minimal shell.

To import the full upstream project tree requested by commit SHA:

```bash
scripts/import_upstream_snapshot.sh \
  https://github.com/Akhileshal96/Trident-Trade-Bot-TL \
  0e749c7364649ece65c46f8b8f5b023f040bf82a
```

Notes:
- The script performs a fresh clone, checks out the exact commit SHA, then rsyncs all files into the current repository root (excluding `.git`).
- If your environment blocks GitHub egress (HTTP 403), run the script in an environment with GitHub access.
