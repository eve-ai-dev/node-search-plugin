from pathlib import Path

import pytest

from node_index import NodeSearchError, build_index, node_search


def write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_frontmatter_links_backlinks_and_aliases(tmp_path):
    root = tmp_path / "brain"
    cache = tmp_path / "cache.json"
    write(root / "A.md", """---
title: Alpha
tags: [project, ai]
status: active
---
# Intro
Links to [[B|Bee]] and ![[Missing]].""")
    write(root / "B.md", """---
title: Beta
---
Back to [[A#Intro]].""")

    result = node_search(scope=None, query="alpha", tags=["ai"], root=root, cache_path=cache, output_mode="full", include=["frontmatter", "outgoing_links", "incoming_links", "headings"])

    assert result["success"] is True
    assert result["count"] == 1
    a = result["results"][0]
    assert a["path"] == "A.md"
    assert a["frontmatter"]["status"] == "active"
    assert "Intro" in a["headings"]
    assert any(link["resolved_path"] == "B.md" for link in a["outgoing_links"])
    assert any(link["target"] == "Missing" and not link["resolved"] for link in a["outgoing_links"])
    assert a["incoming_links"] == ["B.md"]


def test_malformed_frontmatter_is_structured_error(tmp_path):
    root = tmp_path / "brain"
    cache = tmp_path / "cache.json"
    write(root / "bad.md", """---
foo: [
---
Body""")

    result = node_search(query="bad", root=root, cache_path=cache, output_mode="full", include=["frontmatter"])

    assert result["count"] == 1
    item = result["results"][0]
    assert item["frontmatter_ok"] is False
    assert item["frontmatter_error"]


def test_ambiguous_basename_link_is_explicit(tmp_path):
    root = tmp_path / "brain"
    cache = tmp_path / "cache.json"
    write(root / "one" / "Target.md", "# One")
    write(root / "two" / "Target.md", "# Two")
    write(root / "Source.md", "[[Target]]")

    result = node_search(query="Source", root=root, cache_path=cache, output_mode="full", include=["outgoing_links"])
    links = result["results"][0]["outgoing_links"]

    assert links[0]["resolved"] is False
    assert links[0]["ambiguous"] is True
    assert sorted(links[0]["candidates"]) == ["one/Target.md", "two/Target.md"]


def test_path_traversal_rejected(tmp_path):
    root = tmp_path / "brain"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(NodeSearchError):
        build_index(str(outside), root=root, cache_path=tmp_path / "cache.json")


def test_symlink_escape_is_ignored(tmp_path):
    root = tmp_path / "brain"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    write(outside / "secret.md", "[[Nope]]")
    (root / "link.md").symlink_to(outside / "secret.md")

    result = node_search(path_filter="link", root=root, cache_path=tmp_path / "cache.json")

    assert result["stats"]["total"] == 0


def test_depth_expands_graph(tmp_path):
    root = tmp_path / "brain"
    cache = tmp_path / "cache.json"
    write(root / "A.md", """---
title: Seed
---
[[B]]""")
    write(root / "B.md", "[[C]]")
    write(root / "C.md", "leaf")

    result = node_search(query="Seed", depth=1, expand="outgoing", root=root, cache_path=cache, include=[])
    paths = {item["path"] for item in result["results"]}

    assert paths == {"A.md", "B.md"}


def test_path_filter_filters_results_without_changing_scan_contract(tmp_path):
    root = tmp_path / "brain"
    cache = tmp_path / "cache.json"
    write(root / "projects" / "A.md", """---
title: Seed
---
[[B]]""")
    write(root / "archive" / "B.md", "target")

    result = node_search(path_filter="projects", query="Seed", depth=1, expand="outgoing", root=root, cache_path=cache, include=[])
    paths = {item["path"] for item in result["results"]}

    assert paths == {"projects/A.md", "archive/B.md"}
    assert result["stats"]["total"] == 2


def test_empty_call_rejected_instead_of_dumping_vault(tmp_path):
    root = tmp_path / "brain"
    cache = tmp_path / "cache.json"
    write(root / "A.md", "# A")

    with pytest.raises(NodeSearchError, match="orphan/no-backlink notes"):
        node_search(root=root, cache_path=cache)


def test_query_regex_defaults_on_and_explicit_false_is_honored(tmp_path):
    root = tmp_path / "brain"
    cache = tmp_path / "cache.json"
    write(root / "Wild Project.md", "# Wild Project")
    write(root / "Wild X Project.md", "# Wild X Project")
    write(root / "Other.md", "# Other")

    default_regex = node_search(query="Wild Project", where=["path", "basename"], root=root, cache_path=cache, include=[])
    assert {item["path"] for item in default_regex["results"]} == {"Wild Project.md"}

    literal = node_search(query="Wild Project", query_regex=False, where=["path", "basename"], root=root, cache_path=cache, include=[])
    assert {item["path"] for item in literal["results"]} == {"Wild Project.md"}


def test_query_regex_matches_indexed_fields(tmp_path):
    root = tmp_path / "brain"
    cache = tmp_path / "cache.json"
    write(root / "market_radar.md", """---
title: Market Radar
---
# Alpha""")
    write(root / "massive_source.md", """---
title: Massive source
---
# Beta""")
    write(root / "other.md", """---
title: Banana
---
# Gamma""")

    result = node_search(query="market[-_ ]radar|massive", query_regex=True, where=["path", "basename", "frontmatter"], root=root, cache_path=cache, include=[])
    paths = {item["path"] for item in result["results"]}

    assert paths == {"market_radar.md", "massive_source.md"}


def test_invalid_query_regex_returns_user_error(tmp_path):
    root = tmp_path / "brain"
    cache = tmp_path / "cache.json"
    write(root / "A.md", "# A")

    with pytest.raises(NodeSearchError, match="Invalid query_regex"):
        node_search(query="[", query_regex=True, root=root, cache_path=cache)


def test_link_state_orphan_and_unresolved_filters(tmp_path):
    root = tmp_path / "brain"
    cache = tmp_path / "cache.json"
    write(root / "Orphan.md", "[[Missing]]")
    write(root / "Linked.md", "[[Target]]")
    write(root / "Target.md", "target")

    orphans = node_search(link_state=["orphan"], root=root, cache_path=cache, output_mode="full", include=["incoming_links"])
    orphan_paths = {item["path"] for item in orphans["results"]}
    assert "Orphan.md" in orphan_paths
    assert "Linked.md" in orphan_paths
    assert "Target.md" not in orphan_paths

    unresolved = node_search(link_state=["unresolved"], root=root, cache_path=cache, output_mode="full", include=["outgoing_links"])
    assert [item["path"] for item in unresolved["results"]] == ["Orphan.md"]
    assert unresolved["results"][0]["matched"] == ["link_state.unresolved"]


def test_link_state_ambiguous_and_invalid_state(tmp_path):
    root = tmp_path / "brain"
    cache = tmp_path / "cache.json"
    write(root / "one" / "Target.md", "# One")
    write(root / "two" / "Target.md", "# Two")
    write(root / "Source.md", "[[Target]]")

    ambiguous = node_search(link_state=["ambiguous"], root=root, cache_path=cache, output_mode="full", include=["outgoing_links"])
    assert [item["path"] for item in ambiguous["results"]] == ["Source.md"]
    assert ambiguous["results"][0]["matched"] == ["link_state.ambiguous"]

    with pytest.raises(NodeSearchError, match="Invalid link_state"):
        node_search(link_state=["lolno"], root=root, cache_path=cache)


def test_default_excludes_runtime_noise_and_can_be_disabled(tmp_path):
    root = tmp_path / "brain"
    cache = tmp_path / "cache.json"
    write(root / "live" / "A.md", "# A")
    write(root / ".trash" / "Trash.md", "# Trash")
    write(root / ".pytest_cache" / "Cache.md", "# Cache")

    defaulted = node_search(mode="graph_health", link_state=["orphan"], root=root, cache_path=cache, include=[])
    assert {item["path"] for item in defaulted["results"]} == {"live/A.md"}

    explicit = node_search(mode="graph_health", link_state=["orphan"], exclude_defaults=False, root=root, cache_path=cache, include=[])
    assert {item["path"] for item in explicit["results"]} == {".pytest_cache/Cache.md", ".trash/Trash.md", "live/A.md"}


def test_exclude_path_filter_applies_to_seeds_and_expansion(tmp_path):
    root = tmp_path / "brain"
    cache = tmp_path / "cache.json"
    write(root / "projects" / "A.md", """---
title: Seed
---
[[archive/B]]""")
    write(root / "archive" / "B.md", "target")

    result = node_search(query="Seed", depth=1, expand="outgoing", exclude_path_filter=["archive/"], root=root, cache_path=cache, include=[])
    assert {item["path"] for item in result["results"]} == {"projects/A.md"}


def test_mode_and_why_read_are_intent_hints_without_priority_bias(tmp_path):
    root = tmp_path / "brain"
    cache = tmp_path / "cache.json"
    write(root / "B.md", "# B")
    write(root / "A.md", """---
title: Alpha
tags: [x]
---
# Heading
[[B]]""")

    result = node_search(mode="topic_context", query="Alpha", root=root, cache_path=cache, include=["why_read", "headings"])
    assert result["mode"] == "topic_context"
    assert result["results"][0]["path"] == "A.md"
    assert "mode.topic_context" in result["results"][0]["matched"]
    assert any("mode=topic_context" == reason for reason in result["results"][0]["why_read"])

    with pytest.raises(NodeSearchError, match="mode must be one of"):
        node_search(mode="priority", root=root, cache_path=cache)


def test_cached_index_preserves_resolved_graph_without_re_resolving(tmp_path, monkeypatch):
    root = tmp_path / "brain"
    cache = tmp_path / "cache.json"
    write(root / "A.md", "[[B]] and [[Missing]]")
    write(root / "B.md", "[[A]]")

    first = node_search(query="A", root=root, cache_path=cache, output_mode="full", include=["outgoing_links", "incoming_links"], refresh=True)
    assert first["results"][0]["incoming_links"] == ["B.md"]
    assert any(link["resolved_path"] == "B.md" for link in first["results"][0]["outgoing_links"])

    def explode(_nodes):
        raise AssertionError("warm cache should reuse resolved graph without _resolve_links")

    monkeypatch.setattr("node_index._resolve_links", explode)
    second = node_search(query="A", root=root, cache_path=cache, output_mode="full", include=["outgoing_links", "incoming_links"])

    assert second["stats"]["parsed"] == 0
    assert second["stats"].get("graph_reused") is True
    assert second["results"][0]["incoming_links"] == ["B.md"]
    assert any(link["resolved_path"] == "B.md" for link in second["results"][0]["outgoing_links"])


def test_in_process_cache_reuses_nodes_and_invalidates_on_file_change(tmp_path, monkeypatch):
    root = tmp_path / "brain"
    cache = tmp_path / "cache.json"
    write(root / "A.md", """---
title: Alpha
---
[[B]]""")
    write(root / "B.md", "# B")

    first = node_search(query="Alpha", root=root, cache_path=cache, include=[], refresh=True)
    assert first["stats"]["parsed"] == 2

    second = node_search(query="Alpha", root=root, cache_path=cache, include=[])
    assert second["stats"]["parsed"] == 0
    assert second["stats"].get("memory_reused") is True

    def explode_load(_cache_path):
        raise AssertionError("changed file should force JSON reload after invalidating in-process cache")

    monkeypatch.setattr("node_index._load_cache", explode_load)
    second = node_search(query="Alpha", root=root, cache_path=cache, include=[])
    assert second["stats"]["parsed"] == 0
    assert second["stats"].get("memory_reused") is True

    write(root / "B.md", "# B updated")
    third = node_search(query="Alpha", root=root, cache_path=cache, include=[], refresh=True)
    assert third["stats"]["parsed"] == 2


def test_configured_allowed_roots_reject_absolute_escape(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    write(allowed / "A.md", "# A")
    write(outside / "Secret.md", "# Secret")
    monkeypatch.setenv("NODE_SEARCH_ALLOWED_ROOTS", str(allowed))

    with pytest.raises(NodeSearchError, match="outside configured allowed roots"):
        node_search(scope=str(outside), query="Secret", cache_path=tmp_path / "cache.json")


def test_absolute_scope_inside_configured_allowed_root_selects_root(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    sub = allowed / "vault"
    write(sub / "A.md", "# A")
    monkeypatch.setenv("NODE_SEARCH_ALLOWED_ROOTS", str(allowed))

    result = node_search(scope=str(sub), query="A", cache_path=tmp_path / "cache.json")

    assert result["success"] is True
    assert result["root"] == str(allowed.resolve())
    assert result["base"] == "vault"
    assert result["results"][0]["path"] == "vault/A.md"


def test_compact_default_summarizes_frontmatter_and_link_counts(tmp_path):
    root = tmp_path / "brain"
    cache = tmp_path / "cache.json"
    write(root / "A.md", """---
title: Alpha
status: open
note_type: request
summary: Useful context
source: private-source
url: https://example.invalid/private
tags: [ai, market]
---
[[B]] and [[Missing]]""")
    write(root / "B.md", "# B")

    result = node_search(query="Alpha", root=root, cache_path=cache)

    assert result["result_schema_version"] == "node_search.compact.v1"
    assert result["output_mode"] == "compact"
    item = result["results"][0]
    assert item["frontmatter_summary"]["status"] == "open"
    assert item["frontmatter_summary"]["summary"] == "Useful context"
    assert "source" not in item["frontmatter_summary"]
    assert "url" not in item["frontmatter_summary"]
    assert "frontmatter" not in item
    assert "outgoing_links" not in item
    assert item["links"] == {
        "incoming_count": 0,
        "outgoing_count": 2,
        "resolved_count": 1,
        "unresolved_count": 1,
        "ambiguous_count": 0,
    }
    assert item["why_read"]


def test_full_output_mode_preserves_legacy_payload(tmp_path):
    root = tmp_path / "brain"
    cache = tmp_path / "cache.json"
    write(root / "A.md", """---
title: Alpha
status: open
---
[[B]]""")
    write(root / "B.md", "# B")

    result = node_search(query="Alpha", root=root, cache_path=cache, output_mode="full")

    assert result["result_schema_version"] == "node_search.full.v1"
    item = result["results"][0]
    assert item["frontmatter"]["status"] == "open"
    assert item["outgoing_links"][0]["resolved_path"] == "B.md"
    assert item["incoming_links"] == []


def test_frontmatter_fields_override_preset_and_truncate_values(tmp_path):
    root = tmp_path / "brain"
    cache = tmp_path / "cache.json"
    write(root / "A.md", """---
title: Alpha
summary: "abcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyz"
status: open
---
# A""")

    result = node_search(
        query="Alpha",
        root=root,
        cache_path=cache,
        frontmatter_preset="state",
        frontmatter_fields=["summary"],
        max_frontmatter_value_chars=40,
    )

    item = result["results"][0]
    assert list(item["frontmatter_summary"].keys()) == ["summary"]
    assert item["frontmatter_summary"]["summary"].endswith("…")
    assert item["truncated_frontmatter_fields"] == ["summary"]


def test_link_samples_and_graph_health_summary(tmp_path):
    root = tmp_path / "brain"
    cache = tmp_path / "cache.json"
    write(root / "A.md", "[[B]] and [[Missing]]")
    write(root / "B.md", "[[A]]")

    result = node_search(link_state=["unresolved"], root=root, cache_path=cache, link_detail="samples", link_sample_limit=1)

    assert result["graph_health_summary"]["top_unresolved_targets"] == ["Missing"]
    links = result["results"][0]["links"]
    assert links["outgoing_count"] == 2
    assert len(links["outgoing_samples"]) == 1


def test_include_empty_returns_base_only_compact(tmp_path):
    root = tmp_path / "brain"
    cache = tmp_path / "cache.json"
    write(root / "A.md", """---
title: Alpha
status: open
---
[[B]]""")
    write(root / "B.md", "# B")

    result = node_search(query="Alpha", root=root, cache_path=cache, include=[])
    item = result["results"][0]

    assert "frontmatter_summary" not in item
    assert "links" not in item
    assert "why_read" not in item
    assert item["path"] == "A.md"


def test_limit_truncation_reports_omitted_matches(tmp_path):
    root = tmp_path / "brain"
    cache = tmp_path / "cache.json"
    write(root / "A.md", "# Alpha")
    write(root / "B.md", "# Alpha")
    write(root / "C.md", "# Alpha")

    result = node_search(query="Alpha", where=["headings"], root=root, cache_path=cache, limit=1, include=[])

    assert result["count"] == 1
    assert result["truncated"] is True
    assert result["omitted_results_count"] == 2


def test_response_cap_reports_omitted_results(tmp_path):
    root = tmp_path / "brain"
    cache = tmp_path / "cache.json"
    for idx in range(5):
        write(root / f"A{idx}.md", f"""---
title: Alpha {idx}
summary: {"x" * 300}
---
# Alpha""")

    result = node_search(query="Alpha", root=root, cache_path=cache, limit=5, max_total_chars=2000)

    assert result["response_capped"] is True
    assert result["truncated"] is True
    assert result["omitted_results_count"] > 0


def test_evidence_none_and_link_detail_full(tmp_path):
    root = tmp_path / "brain"
    cache = tmp_path / "cache.json"
    write(root / "A.md", "[[B]]")
    write(root / "B.md", "# B")

    result = node_search(query="A", root=root, cache_path=cache, link_detail="full", evidence_detail="none")
    item = result["results"][0]

    assert "why_read" not in item
    assert item["links"]["outgoing"][0]["resolved_path"] == "B.md"


def test_plugin_yaml_default_override_loading(tmp_path, monkeypatch):
    import node_index

    plugin_yaml = tmp_path / "plugin.yaml"
    plugin_yaml.write_text("""defaults:
  output:
    frontmatter_fields: [source]
    evidence_detail: none
""", encoding="utf-8")
    monkeypatch.setattr(node_index, "_PLUGIN_CONFIG_CACHE", None)
    monkeypatch.setattr(node_index, "_plugin_yaml_path", lambda: plugin_yaml)
    root = tmp_path / "brain"
    cache = tmp_path / "cache.json"
    write(root / "A.md", """---
title: Alpha
source: configured
status: open
---
# A""")

    result = node_search(query="Alpha", root=root, cache_path=cache)

    assert result["frontmatter_fields_used"] == ["source"]
    assert result["results"][0]["frontmatter_summary"] == {"source": "configured"}
    assert "why_read" not in result["results"][0]


def test_handler_forwards_compact_output_args(tmp_path, monkeypatch):
    import importlib.util
    import json

    root = tmp_path / "brain"
    monkeypatch.setenv("NODE_SEARCH_ALLOWED_ROOTS", str(root))
    write(root / "A.md", """---
title: Alpha
status: open
source: hidden
---
# A""")
    spec = importlib.util.spec_from_file_location("node_search_plugin_under_test", Path(__file__).with_name("__init__.py"))
    plugin = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(plugin)
    data = json.loads(plugin._handler({
        "scope": str(root),
        "query": "Alpha",
        "frontmatter_fields": ["source"],
        "output_mode": "compact",
        "limit": 1,
    }))

    assert data["success"] is True
    assert data["results"][0]["frontmatter_summary"] == {"source": "hidden"}
