# Catalog and media

- Keep published theme and creator IDs stable. Store a published theme update in a new numbered revision after its format is released. Write an index entry and its corresponding manifest together.
- Keep compatible schema changes in V1. For incompatible changes, coordinate a new schema version with Android reader support and preserve V1 downloads.
- Update SHA-256 values for the exact bytes stored in a manifest or index. Use lowercase UUIDs, safe relative paths and normalized WebP previews.
- Maintain the base Normal and Glass preview coverage documented in the [format guide](../demo/examples/theme-format.md). Use the playback scenes to show active playback in the full player and mini-player.
- Use demo data for captures. Exclude real library data, audio, accounts and private information. Keep runtime demo assets, examples and catalog submissions in their respective folders.
- Keep author verification based on the GitHub account ID in `catalog/creators/verified.json`. Keep report text and publication metadata within the scope of the submission.
