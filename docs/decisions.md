# Decisions

## Release discovery is metadata-only

The project has no published releases or tags yet, and its existing updater pulls and restarts source checkouts. Release discovery therefore uses the canonical GitHub HTTPS API with a bounded response and never installs or executes fetched content. Stable releases are the default, prereleases require an explicit option, and no release is a normal result. Later installation work must establish immutable content verification and a separate user consent step.
