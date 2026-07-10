"""Third-party importers (Phase 4 M4.2).

Three format adapters (`instapaper`, `omnivore`, and — in Task 5 — `readwise`)
over one shared core (`base.run_import`). Each adapter exposes
`parse_export(path) -> Iterator[ImportItem]`, lenient per spec D7.5 (unknown
fields ignored, malformed rows skipped with a logged warning).
"""
