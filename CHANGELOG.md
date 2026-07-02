# Changelog

## Unreleased

- Add compact raw output as the default result schema (`node_search.compact.v1`): `frontmatter_summary`, link counts, capped evidence, and response cap metadata.
- Add explicit `output_mode: "full"` / legacy include escape hatches for raw frontmatter and link arrays.
- Add plugin YAML output defaults plus per-call overrides for frontmatter presets/fields, link detail, evidence detail, and result caps.
- Add graph-health summary metadata for graph-health/link-state calls.
- Adopt public agent-managed repository metadata.
