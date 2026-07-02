"""
node-search — Hermes plugin v0.3.0.
Bounded Markdown metadata+graph prefilter for configured local roots.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

try:
    from .node_index import NodeSearchError, node_search
except ImportError:  # Allows pytest/import smoke tests from the plugin directory.
    import sys
    from pathlib import Path

    plugin_dir = str(Path(__file__).resolve().parent)
    if plugin_dir not in sys.path:
        sys.path.insert(0, plugin_dir)
    from node_index import NodeSearchError, node_search


TOOL_DESCRIPTION = (
    "node_search: configured Markdown/Obsidian context gather. Defaults to compact_raw output: frontmatter_summary + link counts + why_read. Use output_mode='full' only for debugging/raw inspection. Regex is the default search mode over indexed fields; use query_regex=false only when you need literal matching. "
    "Indexed fields: path, basename/title/stem, frontmatter, links, headings. Not a Markdown body search. "
    "Examples: {scope:'obsidian-vault', query:'Wild Project', where:['path','basename','frontmatter','links'], depth:1, limit:10}; "
    "{scope:'obsidian-vault', query:'^(50_work|80_market)/.*(midas|wobi)', where:['path'], include:['why_read'], limit:20}; "
    "{scope:'obsidian-vault', mode:'graph_health', link_state:['orphan'], include:['incoming_links','why_read'], limit:25}. "
    "Use short literal or regex seeds; avoid long semantic-soup queries. If no results, retry once shorter/more targeted, then use search_files for exact body snippets, tables, scripts, transcripts, or non-Markdown files. "
    "scope must stay inside NODE_SEARCH_ALLOWED_ROOTS / NODE_SEARCH_ROOT."
)


def _schema() -> Dict[str, Any]:
    return {
        "name": "node_search",
        "description": TOOL_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "description": "Scan root under the configured allowed root. Use a vault folder such as 'obsidian-vault' or a narrower subtree when you already know the domain. This sets the graph to index; it does not filter results. Absolute paths must stay inside NODE_SEARCH_ALLOWED_ROOTS / NODE_SEARCH_ROOT.",
                },
                "path_filter": {
                    "type": "string",
                    "description": "Substring filter on returned candidates after indexing, e.g. '70_intelligence_layer' or 'market_radar'. Use it to narrow scope without hiding the rest of the indexed graph from link resolution.",
                },
                "path": {
                    "type": "string",
                    "description": "Deprecated alias for scope kept for compatibility. Prefer scope for scan subtree or path_filter for result filtering.",
                },
                "exclude_path_filter": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Exclude path substrings such as ['archive/', 'request_dump'] when broad graph-health queries are noisy.",
                },
                "exclude_defaults": {
                    "type": "boolean",
                    "default": True,
                    "description": "Exclude obvious vault/runtime noise by default: .trash, .pytest_cache, .hermes, __pycache__. Set false only when intentionally auditing those folders.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["auto", "topic_context", "graph_health", "metadata", "link_neighborhood"],
                    "default": "auto",
                    "description": "Intent hint only. topic_context for topic/project/entity candidates; graph_health for orphan/unresolved/ambiguous link audits; metadata for YAML/frontmatter; link_neighborhood for backlink/outgoing-link context.",
                },
                "query": {
                    "type": "string",
                    "description": "Search seed over indexed fields only. Regex is the default when query is provided. Prefer short literal or regex seeds; use search_files for exact body text.",
                    "default": "",
                },
                "query_regex": {
                    "type": "boolean",
                    "default": True,
                    "description": "Regex mode over indexed fields. Defaults to true whenever query is set, unless query_regex=false is passed explicitly. IGNORECASE is enabled. Invalid regex returns a structured error. Requires a non-empty query.",
                },
                "where": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["path", "basename", "frontmatter", "links", "headings"]},
                    "description": "Indexed fields searched by query/regex. Default is all fields. Use ['path'] for folder/path matching, ['basename'] for note-name/title matching, ['links'] for graph/entity links, ['frontmatter'] for YAML metadata, ['headings'] for section-level hints.",
                },
                "frontmatter": {
                    "type": "object",
                    "description": "Exact YAML frontmatter filters before read_file. Dotted keys support nested mappings, e.g. {'project.status': 'active'} or {'status':'open'}. List values require all listed items to be present.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Require all listed YAML frontmatter tags. Inline body hashtags are out of scope for v1.",
                },
                "has_links_to": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Require notes with outgoing wikilinks whose target, alias, or resolved path contains each listed string.",
                },
                "linked_from": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Require notes that receive backlinks from paths containing each listed string.",
                },
                "link_state": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["orphan", "no_backlinks", "unresolved", "dangling", "ambiguous", "has_outgoing", "has_incoming"]},
                    "description": "Graph-health filter: orphan/no_backlinks, unresolved/dangling, ambiguous, or has_outgoing/has_incoming.",
                },
                "include": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["frontmatter_summary", "link_counts", "frontmatter", "outgoing_links", "incoming_links", "headings", "stats", "why_read"],
                    },
                    "description": "Choose only the evidence you need; read_file after selecting candidates. Compact fields: frontmatter_summary, link_counts, why_read, headings, stats. Legacy/full fields: frontmatter, outgoing_links, incoming_links.",
                },
                "output_mode": {
                    "type": "string",
                    "enum": ["compact", "full"],
                    "description": "Result projection. compact is default and token-bounded; full returns legacy raw frontmatter/link arrays for debugging.",
                },
                "frontmatter_preset": {
                    "type": "string",
                    "enum": ["default", "identity", "state", "routing", "source", "all"],
                    "description": "Compact frontmatter_summary preset. default excludes noisier source/url fields; source/all are opt-in and still capped.",
                },
                "frontmatter_fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Explicit frontmatter_summary fields. Overrides frontmatter_preset. Only present keys are returned.",
                },
                "link_detail": {
                    "type": "string",
                    "enum": ["counts", "samples", "full"],
                    "description": "Compact links object detail. counts is default; samples adds capped incoming/outgoing samples; full nests capped full links under links without using legacy field names.",
                },
                "link_sample_limit": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 20,
                    "description": "Sample count when link_detail=samples. Plugin YAML default is normally 5.",
                },
                "evidence_detail": {
                    "type": "string",
                    "enum": ["none", "basic", "expanded"],
                    "description": "Controls compact evidence. basic includes why_read when requested/defaulted; none suppresses it.",
                },
                "max_frontmatter_value_chars": {
                    "type": "integer",
                    "minimum": 40,
                    "maximum": 1000,
                    "description": "Per-frontmatter-value cap in compact summaries.",
                },
                "max_chars_per_result": {
                    "type": "integer",
                    "minimum": 500,
                    "maximum": 20000,
                    "description": "Approximate JSON character cap per result; emits result_capped/truncated_fields when applied.",
                },
                "max_total_chars": {
                    "type": "integer",
                    "minimum": 2000,
                    "maximum": 100000,
                    "description": "Approximate JSON character cap across results; emits response_capped/omitted_results_count when applied.",
                },
                "depth": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 2,
                    "default": 0,
                    "description": "Graph expansion depth. Use depth=1 for immediate neighbors/backlinks. Bounded 0..2 to avoid vault dumps.",
                },
                "expand": {
                    "type": "string",
                    "enum": ["incoming", "outgoing", "both"],
                    "default": "both",
                    "description": "Direction for graph expansion when depth > 0.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 20,
                    "description": "Maximum returned candidates. Hard-capped at 100.",
                },
                "refresh": {
                    "type": "boolean",
                    "default": False,
                    "description": "Force reparsing instead of reusing the JSON cache.",
                },
            },
            "required": ["scope"],
        },
    }


def _handler(args: Optional[Dict[str, Any]] = None, **kwargs: Any) -> str:
    if args is None:
        args = {}
    if not isinstance(args, dict):
        args = {"query": str(args)}
    args = {**args, **kwargs}
    try:
        data = node_search(
            scope=args.get("scope") or args.get("path"),
            path_filter=args.get("path_filter"),
            exclude_path_filter=args.get("exclude_path_filter"),
            exclude_defaults=bool(args.get("exclude_defaults", True)),
            mode=args.get("mode") or "auto",
            query=args.get("query") or "",
            query_regex=_resolve_query_regex(args),
            where=args.get("where"),
            frontmatter=args.get("frontmatter"),
            tags=args.get("tags"),
            has_links_to=args.get("has_links_to"),
            linked_from=args.get("linked_from"),
            link_state=args.get("link_state"),
            include=args.get("include"),
            depth=int(args.get("depth") or 0),
            expand=args.get("expand") or "both",
            limit=int(args.get("limit") or 20),
            refresh=bool(args.get("refresh", False)),
            output_mode=args.get("output_mode"),
            frontmatter_preset=args.get("frontmatter_preset"),
            frontmatter_fields=args.get("frontmatter_fields"),
            link_detail=args.get("link_detail"),
            link_sample_limit=args.get("link_sample_limit"),
            evidence_detail=args.get("evidence_detail"),
            max_frontmatter_value_chars=args.get("max_frontmatter_value_chars"),
            max_chars_per_result=args.get("max_chars_per_result"),
            max_total_chars=args.get("max_total_chars"),
        )
    except NodeSearchError as exc:
        data = {"success": False, "error": str(exc), "results": []}
    except Exception as exc:
        data = {"success": False, "error": f"node_search internal error: {type(exc).__name__}: {exc}", "results": []}
    return json.dumps(data, ensure_ascii=False, indent=2)


def _resolve_query_regex(args: Dict[str, Any]) -> bool:
    if "query_regex" not in args or args.get("query_regex") is None:
        return bool((args.get("query") or "").strip())
    return bool(args.get("query_regex"))


def register(ctx: Any) -> None:
    ctx.register_tool(
        name="node_search",
        toolset="file",
        schema=_schema(),
        handler=_handler,
        check_fn=lambda: True,
        requires_env=[],
        description="First-pass Markdown context gather: regex-default frontmatter/link-graph candidate selector before read_file",
        emoji="🕸️",
    )
