# Decisions

## Release discovery is metadata-only

The project has no published releases or tags yet, and its existing updater pulls and restarts source checkouts. Release discovery therefore uses the canonical GitHub HTTPS API with a bounded response and never installs or executes fetched content. Stable releases are the default, prereleases require an explicit option, and no release is a normal result. Later installation work must establish immutable content verification and a separate user consent step.

## Scheduled checks only offer release metadata

The timer may fetch bounded canonical release metadata, but it must not update installed code or restart the relay. It writes a private, validated offer without marking it offered; MCP initialization reads only that local cache and offers a version notice to the client once per version, while release notes remain available only from an explicit `check_updates` call. This records delivery to the client, not proof that a human read it. The updater script stays a separate manual source-checkout operation until an activation and consent design is ready. Owners must rerun the installer to replace timer definitions installed by an older version.

## A foreach step matches fail patterns per item

`failIfOutputMatches` used to be tested against the step's aggregated output, which for a `foreach` step is a JSON array of the item outputs. An anchored pattern then either could not see what an agent reported (`^BLOCKED` never matches a report quoted inside the array) or fired on the serialization itself (`^\[` failed every healthy fan-out). The pattern is now tested against each completed task's own raw output, before the step may complete: one matching item fails the whole step with `output_matched_fail_pattern` and its dependents are skipped, and the step still keeps the JSON array as its output for a caller to inspect. Consequence: matching is per item, so an aggregate-shaped pattern (`^\[`) no longer has a meaning on a `foreach` step, while the other terminal causes keep their precedence — a task that failed, timed out or was cancelled still decides the step before the pattern is read, and `retries`, `repeat` and journals written before the field existed behave exactly as before.
