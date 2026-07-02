"""Core indexer for node_search: bounded Markdown frontmatter/link graph search."""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import yaml

def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home()).expanduser().resolve()
    except Exception:
        return Path(os.getenv("HERMES_HOME", Path.home() / ".hermes")).expanduser().resolve()


def _split_paths(value: str) -> List[Path]:
    return [Path(part).expanduser() for part in value.split(",") if part.strip()]


def _plugin_yaml_path() -> Path:
    return Path(__file__).resolve().parent / "plugin.yaml"


def _load_plugin_output_config() -> Dict[str, Any]:
    global _PLUGIN_CONFIG_CACHE
    if _PLUGIN_CONFIG_CACHE is not None:
        return dict(_PLUGIN_CONFIG_CACHE)
    config = dict(PLUGIN_DEFAULT_CONFIG)
    try:
        raw = yaml.safe_load(_plugin_yaml_path().read_text(encoding="utf-8")) or {}
    except Exception:
        raw = {}
    if isinstance(raw, dict) and isinstance(raw.get("defaults"), dict):
        output_defaults = raw["defaults"].get("output")
        if isinstance(output_defaults, dict):
            config.update(output_defaults)
    _PLUGIN_CONFIG_CACHE = config
    return dict(config)


def _as_str_list(value: Any) -> List[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(part).strip() for part in value if str(part).strip()]
    return [str(value).strip()]


def _clean_enum(name: str, value: Any, allowed: Sequence[str], default: str) -> str:
    candidate = str(value or default).strip().lower()
    if candidate not in set(allowed):
        raise NodeSearchError(f"{name} must be one of: {', '.join(allowed)}")
    return candidate


def _clean_int(name: str, value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value if value is not None else default)
    except (TypeError, ValueError) as exc:
        raise NodeSearchError(f"{name} must be an integer") from exc
    return max(minimum, min(parsed, maximum))


def _output_options(
    *,
    output_mode: str | None = None,
    frontmatter_preset: str | None = None,
    frontmatter_fields: Optional[Sequence[str]] = None,
    link_detail: str | None = None,
    link_sample_limit: int | None = None,
    evidence_detail: str | None = None,
    max_frontmatter_value_chars: int | None = None,
    max_chars_per_result: int | None = None,
    max_total_chars: int | None = None,
) -> Dict[str, Any]:
    defaults = _load_plugin_output_config()
    opts = {
        "output_mode": _clean_enum("output_mode", output_mode or defaults.get("output_mode"), ["compact", "full"], "compact"),
        "frontmatter_preset": _clean_enum(
            "frontmatter_preset",
            frontmatter_preset or defaults.get("frontmatter_preset"),
            ["default", "identity", "state", "routing", "source", "all"],
            "default",
        ),
        "frontmatter_fields": _as_str_list(frontmatter_fields if frontmatter_fields is not None else defaults.get("frontmatter_fields")),
        "link_detail": _clean_enum("link_detail", link_detail or defaults.get("link_detail"), ["counts", "samples", "full"], "counts"),
        "link_sample_limit": _clean_int("link_sample_limit", link_sample_limit if link_sample_limit is not None else defaults.get("link_sample_limit"), 5, minimum=0, maximum=20),
        "evidence_detail": _clean_enum("evidence_detail", evidence_detail or defaults.get("evidence_detail"), ["none", "basic", "expanded"], "basic"),
        "max_frontmatter_value_chars": _clean_int("max_frontmatter_value_chars", max_frontmatter_value_chars if max_frontmatter_value_chars is not None else defaults.get("max_frontmatter_value_chars"), 240, minimum=40, maximum=1000),
        "max_chars_per_result": _clean_int("max_chars_per_result", max_chars_per_result if max_chars_per_result is not None else defaults.get("max_chars_per_result"), 2000, minimum=500, maximum=20000),
        "max_total_chars": _clean_int("max_total_chars", max_total_chars if max_total_chars is not None else defaults.get("max_total_chars"), 12000, minimum=2000, maximum=100000),
    }
    return opts


def allowed_roots() -> List[Path]:
    """Return configured candidate roots without requiring them to exist."""
    configured = os.getenv("NODE_SEARCH_ALLOWED_ROOTS", "").strip()
    legacy = os.getenv("NODE_SEARCH_ROOT", "").strip()
    roots = _split_paths(configured) if configured else []
    if legacy:
        roots.insert(0, Path(legacy).expanduser())
    if not roots:
        roots = [_hermes_home() / "brain"]
        # Compatibility for Alberto's Docker runtime without making it the public default.
        if _hermes_home() == Path("/opt/data") and Path("/opt/data/brain").exists():
            roots = [Path("/opt/data/brain")]
    return roots


def default_root() -> Path:
    roots = allowed_roots()
    if not roots:
        raise RuntimeError("NODE_SEARCH_ALLOWED_ROOTS is empty; configure at least one allowed Markdown root")
    return roots[0]


def default_cache() -> Path:
    configured = os.getenv("NODE_SEARCH_CACHE", "").strip()
    if configured:
        return Path(configured).expanduser()
    return _hermes_home() / ".cache" / "hermes" / "node_search" / "index.json"


def _root_policy_is_configured() -> bool:
    return bool(os.getenv("NODE_SEARCH_ALLOWED_ROOTS", "").strip() or os.getenv("NODE_SEARCH_ROOT", "").strip())


def _existing_allowed_roots() -> List[Path]:
    return [p.expanduser().resolve() for p in allowed_roots() if p.expanduser().exists()]


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _select_root_for_scope(scope: str | None, root: Path | None) -> Path:
    if root is not None:
        return Path(root)
    if scope:
        requested = Path(scope).expanduser()
        if requested.is_absolute():
            resolved = requested.resolve()
            for allowed in _existing_allowed_roots():
                if _is_inside(resolved, allowed):
                    return allowed
            allowed_text = ", ".join(str(p) for p in allowed_roots())
            raise NodeSearchError(f"Path is outside configured allowed roots: {scope}. Current allowed roots: {allowed_text}")
    return default_root()


DEFAULT_ROOT = default_root()
DEFAULT_CACHE = default_cache()
MAX_RESULTS = 100
MAX_DEPTH = 2
MAX_LINKS_PER_NODE = 80
MAX_FRONTMATTER_CHARS = 6000
MAX_HEADING_CHARS = 4000
DEFAULT_EXCLUDE_PATHS = ("/.trash/", "/.pytest_cache/", "/.hermes/", "/__pycache__/")
_PROCESS_INDEX_CACHE: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
RESULT_SCHEMA_COMPACT_V1 = "node_search.compact.v1"
RESULT_SCHEMA_FULL_V1 = "node_search.full.v1"
DEFAULT_FRONTMATTER_PRESETS: Dict[str, List[str]] = {
    "identity": ["title", "aliases", "note_type", "type"],
    "state": ["status", "priority", "approval_state", "phase", "confidence"],
    "routing": ["tags", "owner", "domain", "client", "project_slug", "topic_id"],
    "source": ["source", "source_refs", "url", "author", "saved_at"],
}
DEFAULT_FRONTMATTER_PRESETS["default"] = [
    "status",
    "note_type",
    "type",
    "summary",
    "tags",
    "priority",
    "owner",
    "domain",
    "client",
    "project_slug",
    "topic_id",
    "approval_state",
    "phase",
    "confidence",
    "updated_at",
    "updated",
    "last_reviewed",
    "stale_after",
    "created_at",
    "created",
    "aliases",
]
DEFAULT_FRONTMATTER_PRESETS["all"] = []
LEGACY_INCLUDE_FIELDS = {"frontmatter", "incoming_links", "outgoing_links"}
COMPACT_INCLUDE_FIELDS = {"frontmatter_summary", "link_counts", "why_read", "headings", "stats"}
PLUGIN_DEFAULT_CONFIG: Dict[str, Any] = {
    "output_mode": "compact",
    "frontmatter_preset": "default",
    "frontmatter_fields": [],
    "link_detail": "counts",
    "link_sample_limit": 5,
    "evidence_detail": "basic",
    "max_frontmatter_value_chars": 240,
    "max_chars_per_result": 2000,
    "max_total_chars": 12000,
}
_PLUGIN_CONFIG_CACHE: Optional[Dict[str, Any]] = None
EMPTY_CALL_GUIDANCE = (
    "node_search is a first-pass /brain context-gather tool, not a vault dump. "
    "Call it with scope plus a narrowing parameter. Examples: "
    "context around topic X -> {\"scope\":\"obsidian-vault\",\"mode\":\"topic_context\",\"query\":\"X\",\"where\":[\"path\",\"basename\",\"frontmatter\",\"links\"],\"depth\":1,\"limit\":10}; "
    "regex over indexed fields -> {\"scope\":\"obsidian-vault\",\"query\":\"^(50_work|80_market)/.*(midas|wobi)\",\"query_regex\":true,\"where\":[\"path\"],\"include\":[\"why_read\"],\"limit\":20}; "
    "orphan/no-backlink notes -> {\"scope\":\"obsidian-vault\",\"mode\":\"graph_health\",\"link_state\":[\"orphan\"],\"include\":[\"incoming_links\",\"why_read\"],\"limit\":25}; "
    "unresolved/dangling wikilinks -> {\"scope\":\"obsidian-vault\",\"mode\":\"graph_health\",\"link_state\":[\"unresolved\"],\"include\":[\"outgoing_links\",\"why_read\"],\"limit\":25}. "
    "Regex/query searches indexed fields only, not full Markdown body text; use search_files for body prose, tables, transcripts, scripts, non-Markdown files, or exact snippets."
)

WIKILINK_RE = re.compile(r"(!?)\[\[([^\[\]]+?)\]\]")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


class NodeSearchError(ValueError):
    """Expected user/input error for node_search."""


@dataclass
class Link:
    raw: str
    target: str
    alias: Optional[str] = None
    heading: Optional[str] = None
    embed: bool = False
    resolved_path: Optional[str] = None
    resolved: bool = False
    ambiguous: bool = False
    candidates: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "raw": self.raw,
            "target": self.target,
            "alias": self.alias,
            "heading": self.heading,
            "embed": self.embed,
            "resolved_path": self.resolved_path,
            "resolved": self.resolved,
            "ambiguous": self.ambiguous,
        }
        if self.candidates:
            data["candidates"] = self.candidates[:10]
        return data


@dataclass
class Node:
    path: str
    basename: str
    stem: str
    title: str
    mtime: float
    size: int
    frontmatter: Dict[str, Any] = field(default_factory=dict)
    frontmatter_ok: bool = True
    frontmatter_error: Optional[str] = None
    headings: List[str] = field(default_factory=list)
    outgoing_links: List[Link] = field(default_factory=list)
    incoming_links: List[str] = field(default_factory=list)

    def to_cache(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "basename": self.basename,
            "stem": self.stem,
            "title": self.title,
            "mtime": self.mtime,
            "size": self.size,
            "frontmatter": self.frontmatter,
            "frontmatter_ok": self.frontmatter_ok,
            "frontmatter_error": self.frontmatter_error,
            "headings": self.headings,
            "outgoing_links": [link.to_dict() for link in self.outgoing_links],
            "incoming_links": self.incoming_links,
        }

    @classmethod
    def from_cache(cls, data: Dict[str, Any]) -> "Node":
        links = [Link(**link) for link in data.get("outgoing_links", [])]
        return cls(
            path=data["path"],
            basename=data.get("basename", Path(data["path"]).name),
            stem=data.get("stem", Path(data["path"]).stem),
            title=data.get("title") or data.get("stem") or Path(data["path"]).stem,
            mtime=float(data.get("mtime", 0)),
            size=int(data.get("size", 0)),
            frontmatter=data.get("frontmatter") or {},
            frontmatter_ok=bool(data.get("frontmatter_ok", True)),
            frontmatter_error=data.get("frontmatter_error"),
            headings=list(data.get("headings") or []),
            outgoing_links=links,
            incoming_links=list(data.get("incoming_links") or []),
        )


def _resolve_root(root: Path | None = None) -> Path:
    root = Path(root if root is not None else default_root()).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        allowed = ", ".join(str(p) for p in allowed_roots())
        raise NodeSearchError(f"Root does not exist or is not a directory: {root}. Configure NODE_SEARCH_ALLOWED_ROOTS. Current allowed roots: {allowed}")
    if _root_policy_is_configured():
        allowed_existing = _existing_allowed_roots()
        if not any(root == allowed or _is_inside(root, allowed) for allowed in allowed_existing):
            allowed = ", ".join(str(p) for p in allowed_roots())
            raise NodeSearchError(f"Root is outside configured allowed roots: {root}. Current allowed roots: {allowed}")
    return root


def _safe_subpath(root: Path, requested: str | None) -> Path:
    root = _resolve_root(root)
    if not requested:
        return root
    path = Path(requested).expanduser()
    if not path.is_absolute():
        path = root / path
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise NodeSearchError(f"Path is outside allowed root {root}: {requested}") from exc
    if not resolved.exists() or not resolved.is_dir():
        raise NodeSearchError(f"Path does not exist or is not a directory: {resolved}")
    return resolved


def _rel(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root).as_posix()


def _read_frontmatter(text: str) -> Tuple[Dict[str, Any], bool, Optional[str], str]:
    if not text.startswith("---"):
        return {}, True, None, text
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, True, None, text
    end_idx = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() in {"---", "..."}:
            end_idx = i
            break
    if end_idx is None:
        return {}, False, "frontmatter opening delimiter without closing delimiter", text
    raw = "".join(lines[1:end_idx])
    body = "".join(lines[end_idx + 1 :])
    if len(raw) > MAX_FRONTMATTER_CHARS:
        return {}, False, f"frontmatter exceeds {MAX_FRONTMATTER_CHARS} chars", body
    try:
        parsed = yaml.safe_load(raw) if raw.strip() else {}
        if parsed is None:
            parsed = {}
        if not isinstance(parsed, dict):
            return {}, False, f"frontmatter is {type(parsed).__name__}, expected mapping", body
        return parsed, True, None, body
    except Exception as exc:  # yaml raises several subclasses
        return {}, False, str(exc).splitlines()[0][:300], body


def _extract_headings(body: str) -> List[str]:
    headings: List[str] = []
    seen_chars = 0
    for line in body.splitlines():
        m = HEADING_RE.match(line)
        if not m:
            continue
        title = m.group(2).strip().strip("#").strip()
        if not title:
            continue
        seen_chars += len(title)
        if seen_chars > MAX_HEADING_CHARS:
            break
        headings.append(title[:200])
    return headings[:50]


def _parse_wikilink(raw_body: str, match: re.Match[str]) -> Link:
    embed = bool(match.group(1))
    raw_inside = match.group(2).strip()
    alias = None
    target_part = raw_inside
    if "|" in raw_inside:
        target_part, alias = raw_inside.split("|", 1)
        alias = alias.strip() or None
    heading = None
    if "#" in target_part:
        target_part, heading = target_part.split("#", 1)
        heading = heading.strip() or None
    target = target_part.strip()
    return Link(raw=match.group(0), target=target, alias=alias, heading=heading, embed=embed)


def _extract_links(body: str) -> List[Link]:
    links = [_parse_wikilink(body, match) for match in WIKILINK_RE.finditer(body)]
    return links[:MAX_LINKS_PER_NODE]


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except TypeError:
        pass
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return str(value)


def _parse_file(root: Path, path: Path) -> Node:
    st = path.stat()
    rel = _rel(root, path)
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="utf-8", errors="replace")
    frontmatter, ok, error, body = _read_frontmatter(text)
    frontmatter = _json_safe(frontmatter) if isinstance(frontmatter, dict) else {}
    title = str(frontmatter.get("title") or frontmatter.get("name") or path.stem)
    return Node(
        path=rel,
        basename=path.name,
        stem=path.stem,
        title=title,
        mtime=st.st_mtime,
        size=st.st_size,
        frontmatter=frontmatter,
        frontmatter_ok=ok,
        frontmatter_error=error,
        headings=_extract_headings(body),
        outgoing_links=_extract_links(body),
    )


def _load_cache(cache_path: Path) -> Dict[str, Any]:
    try:
        if cache_path.exists():
            return json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {}


def _write_cache(cache_path: Path, payload: Dict[str, Any]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="index.", suffix=".json", dir=str(cache_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, cache_path)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


def _iter_markdown(root: Path, base: Path) -> Iterable[Path]:
    for path in base.rglob("*.md"):
        try:
            resolved = path.resolve()
            resolved.relative_to(root)
        except Exception:
            continue
        if resolved.is_file():
            yield resolved


def _build_path_indexes(nodes: Dict[str, Node]) -> Tuple[Dict[str, str], Dict[str, List[str]]]:
    exact: Dict[str, str] = {}
    by_stem: Dict[str, List[str]] = {}
    for rel, node in nodes.items():
        no_ext = rel[:-3] if rel.endswith(".md") else rel
        exact[no_ext.lower()] = rel
        exact[rel.lower()] = rel
        by_stem.setdefault(node.stem.lower(), []).append(rel)
    return exact, by_stem


def _resolve_links(nodes: Dict[str, Node]) -> None:
    exact, by_stem = _build_path_indexes(nodes)
    backlinks: Dict[str, List[str]] = {rel: [] for rel in nodes}
    for source_rel, node in nodes.items():
        for link in node.outgoing_links:
            link.resolved_path = None
            link.resolved = False
            link.ambiguous = False
            link.candidates = []
            if not link.target:
                continue
            normalized = link.target.strip().replace("\\", "/").strip("/")
            key = normalized[:-3] if normalized.lower().endswith(".md") else normalized
            target = exact.get(key.lower()) or exact.get((key + ".md").lower())
            if target:
                link.resolved_path = target
                link.resolved = True
                backlinks.setdefault(target, []).append(source_rel)
                continue
            candidates = sorted(by_stem.get(Path(key).name.lower(), []))
            if len(candidates) == 1:
                link.resolved_path = candidates[0]
                link.resolved = True
                backlinks.setdefault(candidates[0], []).append(source_rel)
            elif len(candidates) > 1:
                link.ambiguous = True
                link.candidates = candidates[:20]
    for rel, node in nodes.items():
        node.incoming_links = sorted(set(backlinks.get(rel, [])))


def _markdown_signature(root: Path, base: Path) -> Tuple[str, ...]:
    signature: List[str] = []
    for md in _iter_markdown(root, base):
        rel = _rel(root, md)
        st = md.stat()
        signature.append(f"{rel}\0{st.st_mtime_ns}\0{st.st_size}")
    return tuple(signature)


def build_index(
    path: str | None = None,
    *,
    root: Path | None = None,
    cache_path: Path | None = None,
    refresh: bool = False,
) -> Dict[str, Any]:
    root = _resolve_root(_select_root_for_scope(path, root))
    cache_path = Path(cache_path or default_cache()).expanduser().resolve()
    base = _safe_subpath(root, path)
    signature = _markdown_signature(root, base)
    mem_key = (str(root), _rel(root, base), str(Path(cache_path).expanduser().resolve()))
    if not refresh:
        cached_index = _PROCESS_INDEX_CACHE.get(mem_key)
        if cached_index and cached_index.get("signature") == signature:
            nodes = cached_index["nodes"]
            return {
                "root": str(root),
                "base": _rel(root, base),
                "nodes": nodes,
                "stats": {
                    "total": len(nodes),
                    "parsed": 0,
                    "reused": len(nodes),
                    "graph_reused": True,
                    "memory_reused": True,
                },
            }

    cache = {} if refresh else _load_cache(cache_path)
    cached_nodes = cache.get("nodes", {}) if isinstance(cache.get("nodes"), dict) else {}
    nodes: Dict[str, Node] = {}
    parsed = reused = 0

    for entry in signature:
        rel, _mtime_ns, _size = entry.split("\0", 2)
        md = root / rel
        st = md.stat()
        cached = cached_nodes.get(rel)
        if (
            not refresh
            and isinstance(cached, dict)
            and float(cached.get("mtime", -1)) == st.st_mtime
            and int(cached.get("size", -1)) == st.st_size
        ):
            nodes[rel] = Node.from_cache(cached)
            reused += 1
        else:
            nodes[rel] = _parse_file(root, md)
            parsed += 1

    cache_has_resolved_graph = bool(cache.get("resolved_graph"))
    graph_reused = bool(cache_has_resolved_graph and parsed == 0 and len(signature) == len(nodes))
    if not graph_reused:
        _resolve_links(nodes)
    if not graph_reused:
        payload = {
            "version": 2,
            "root": str(root),
            "resolved_graph": True,
            "nodes": {rel: node.to_cache() for rel, node in sorted(nodes.items())},
        }
        _write_cache(cache_path, payload)
    _PROCESS_INDEX_CACHE[mem_key] = {"signature": signature, "nodes": nodes}
    return {
        "root": str(root),
        "base": _rel(root, base),
        "nodes": nodes,
        "stats": {"total": len(nodes), "parsed": parsed, "reused": reused, "graph_reused": graph_reused},
    }


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _flatten(obj: Any, prefix: str = "") -> Iterable[Tuple[str, Any]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            yield from _flatten(v, key)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            key = f"{prefix}[{i}]" if prefix else f"[{i}]"
            yield from _flatten(v, key)
    else:
        yield prefix, obj


def _compile_query_regex(query: str, enabled: bool) -> Optional[re.Pattern[str]]:
    if not enabled or not query:
        return None
    try:
        return re.compile(query, re.IGNORECASE)
    except re.error as exc:
        raise NodeSearchError(f"Invalid query_regex: {exc}") from exc


def _contains(hay: Any, needle: str, regex: Optional[re.Pattern[str]] = None) -> bool:
    text = str(hay)
    if regex is not None:
        return bool(regex.search(text))
    return needle.lower() in text.lower()


def _frontmatter_get(fm: Dict[str, Any], dotted: str) -> Any:
    cur: Any = fm
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _frontmatter_matches(node: Node, filters: Dict[str, Any]) -> Tuple[bool, List[str]]:
    matched: List[str] = []
    for key, expected in filters.items():
        actual = _frontmatter_get(node.frontmatter, key)
        if isinstance(expected, list):
            actual_values = _as_list(actual)
            if all(item in actual_values for item in expected):
                matched.append(f"frontmatter.{key}")
            else:
                return False, matched
        elif actual == expected:
            matched.append(f"frontmatter.{key}")
        else:
            return False, matched
    return True, matched


def _tags(node: Node) -> List[str]:
    tags = node.frontmatter.get("tags")
    values = _as_list(tags)
    out: List[str] = []
    for val in values:
        if isinstance(val, str):
            out.extend([part.strip().lstrip("#") for part in val.replace(",", " ").split() if part.strip()])
    return out


def _field_matches(value: Any, query: str, regex: Optional[re.Pattern[str]]) -> bool:
    return _contains(value, query, regex)


def _query_matches(node: Node, query: str, where: Sequence[str], regex: Optional[re.Pattern[str]] = None) -> Tuple[int, List[str]]:
    if not query:
        return 1, []
    score = 0
    matched: List[str] = []
    fields = set(where or ["path", "basename", "frontmatter", "links", "headings"])
    if "path" in fields and _field_matches(node.path, query, regex):
        score += 30
        matched.append("path")
    if "basename" in fields and (
        _field_matches(node.basename, query, regex)
        or _field_matches(node.title, query, regex)
        or _field_matches(node.stem, query, regex)
    ):
        score += 35
        matched.append("basename")
    if "frontmatter" in fields:
        for key, value in _flatten(node.frontmatter):
            if _field_matches(key, query, regex) or _field_matches(value, query, regex):
                score += 25
                matched.append(f"frontmatter.{key}" if key else "frontmatter")
                break
    if "links" in fields:
        for link in node.outgoing_links:
            if _field_matches(link.target, query, regex) or _field_matches(link.alias, query, regex) or _field_matches(link.resolved_path, query, regex):
                score += 20
                matched.append("outgoing_links")
                break
        if any(_field_matches(incoming, query, regex) for incoming in node.incoming_links):
            score += 20
            matched.append("incoming_links")
    if "headings" in fields and any(_field_matches(h, query, regex) for h in node.headings):
        score += 15
        matched.append("headings")
    return score, matched


def _target_matches_link(link: Link, target: str) -> bool:
    t = target.lower().strip().rstrip("/")
    vals = [link.target, link.resolved_path, link.alias]
    return any(v and t in str(v).lower() for v in vals)


def _normalize_mode(mode: str | None) -> str:
    normalized = (mode or "auto").strip().lower()
    aliases = {
        "context": "topic_context",
        "topic": "topic_context",
        "links": "link_neighborhood",
        "link": "link_neighborhood",
        "graph": "link_neighborhood",
        "health": "graph_health",
        "link_health": "graph_health",
        "frontmatter": "metadata",
        "yaml": "metadata",
    }
    normalized = aliases.get(normalized, normalized)
    allowed = {"auto", "topic_context", "graph_health", "metadata", "link_neighborhood"}
    if normalized not in allowed:
        raise NodeSearchError(f"mode must be one of: {', '.join(sorted(allowed))}")
    return normalized


def _mode_matches(
    node: Node,
    mode: str,
    *,
    query: str,
    frontmatter: Optional[Dict[str, Any]],
    tags: Sequence[str],
    has_links_to: Sequence[str],
    linked_from: Sequence[str],
    link_state: Sequence[str],
) -> bool:
    if mode == "auto":
        return True
    if mode == "metadata":
        return bool(frontmatter or tags or query or node.frontmatter)
    if mode == "graph_health":
        return bool(link_state or any(not link.resolved or link.ambiguous for link in node.outgoing_links) or not node.incoming_links)
    if mode == "link_neighborhood":
        return bool(has_links_to or linked_from or link_state or node.outgoing_links or node.incoming_links)
    if mode == "topic_context":
        return bool(query or frontmatter or tags or has_links_to or linked_from or link_state)
    return True


def _is_excluded_path(rel: str, exclude_terms: Sequence[str]) -> bool:
    normalized = "/" + rel.lower().strip("/") + "/"
    for term in exclude_terms:
        t = str(term).strip().lower().replace("\\", "/")
        if not t:
            continue
        if t.startswith("/") or t.endswith("/"):
            needle = "/" + t.strip("/") + "/"
            if needle in normalized:
                return True
        elif t in rel.lower():
            return True
    return False


def _why_read(node: Node, matched: Sequence[str], mode: str) -> List[str]:
    reasons: List[str] = []
    matched_set = set(matched or [])
    if matched_set:
        reasons.append("matched indexed fields: " + ", ".join(sorted(matched_set))[:160])
    if mode != "auto":
        reasons.append(f"mode={mode}")
    if node.frontmatter:
        keys = sorted(str(k) for k in node.frontmatter.keys())[:8]
        if keys:
            reasons.append("has YAML/frontmatter keys: " + ", ".join(keys))
    if node.incoming_links:
        reasons.append(f"has {len(node.incoming_links)} incoming link(s)")
    else:
        reasons.append("has no incoming links")
    resolved_count = sum(1 for link in node.outgoing_links if link.resolved)
    unresolved_count = sum(1 for link in node.outgoing_links if not link.resolved and not link.ambiguous)
    ambiguous_count = sum(1 for link in node.outgoing_links if link.ambiguous)
    if node.outgoing_links:
        parts = [f"{len(node.outgoing_links)} outgoing link(s)"]
        if resolved_count:
            parts.append(f"{resolved_count} resolved")
        if unresolved_count:
            parts.append(f"{unresolved_count} unresolved")
        if ambiguous_count:
            parts.append(f"{ambiguous_count} ambiguous")
        reasons.append("; ".join(parts))
    if node.headings:
        reasons.append("headings available for section-level triage")
    return reasons[:6]


def _filter_nodes(
    nodes: Dict[str, Node],
    *,
    mode: str = "auto",
    query: str = "",
    where: Sequence[str] = (),
    frontmatter: Optional[Dict[str, Any]] = None,
    tags: Sequence[str] = (),
    has_links_to: Sequence[str] = (),
    linked_from: Sequence[str] = (),
    link_state: Sequence[str] = (),
    query_regex: bool = False,
) -> List[Tuple[Node, int, List[str]]]:
    results: List[Tuple[Node, int, List[str]]] = []
    wanted_tags = {str(t).lstrip("#").lower() for t in tags if str(t).strip()}
    from_terms = [str(x).lower() for x in linked_from if str(x).strip()]
    to_terms = [str(x) for x in has_links_to if str(x).strip()]
    state_terms = {str(x).strip().lower() for x in link_state if str(x).strip()}
    allowed_states = {"orphan", "no_backlinks", "unresolved", "dangling", "ambiguous", "has_outgoing", "has_incoming"}
    invalid_states = sorted(state_terms - allowed_states)
    if invalid_states:
        raise NodeSearchError(f"Invalid link_state values: {', '.join(invalid_states)}")
    regex = _compile_query_regex(query, query_regex)
    for node in nodes.values():
        matched: List[str] = []
        if not _mode_matches(
            node,
            mode,
            query=query,
            frontmatter=frontmatter,
            tags=tags,
            has_links_to=has_links_to,
            linked_from=linked_from,
            link_state=link_state,
        ):
            continue
        if mode != "auto":
            matched.append(f"mode.{mode}")
        score, qmatches = _query_matches(node, query, where, regex)
        if query and score <= 0:
            continue
        matched.extend(qmatches)
        if frontmatter:
            ok, fmatches = _frontmatter_matches(node, frontmatter)
            if not ok:
                continue
            score += 40 * max(1, len(fmatches))
            matched.extend(fmatches)
        if wanted_tags:
            node_tags = {t.lower() for t in _tags(node)}
            if not wanted_tags.issubset(node_tags):
                continue
            score += 30
            matched.append("tags")
        if to_terms:
            if not all(any(_target_matches_link(link, term) for link in node.outgoing_links) for term in to_terms):
                continue
            score += 25
            matched.append("has_links_to")
        if from_terms:
            if not all(any(term in incoming.lower() for incoming in node.incoming_links) for term in from_terms):
                continue
            score += 25
            matched.append("linked_from")
        if state_terms:
            unresolved = [link for link in node.outgoing_links if not link.resolved and not link.ambiguous]
            ambiguous = [link for link in node.outgoing_links if link.ambiguous]
            state_ok = True
            for state in state_terms:
                if state in {"orphan", "no_backlinks"} and node.incoming_links:
                    state_ok = False
                    break
                if state in {"unresolved", "dangling"} and not unresolved:
                    state_ok = False
                    break
                if state == "ambiguous" and not ambiguous:
                    state_ok = False
                    break
                if state == "has_outgoing" and not node.outgoing_links:
                    state_ok = False
                    break
                if state == "has_incoming" and not node.incoming_links:
                    state_ok = False
                    break
            if not state_ok:
                continue
            score += 35 * max(1, len(state_terms))
            matched.extend([f"link_state.{s}" for s in sorted(state_terms)])
        results.append((node, max(score, 1), sorted(set(matched))))
    return sorted(results, key=lambda item: (-item[1], item[0].path.lower()))


def _expand_results(
    seeds: List[Tuple[Node, int, List[str]]],
    nodes: Dict[str, Node],
    depth: int,
    expand: str,
) -> List[Tuple[Node, int, List[str]]]:
    if depth <= 0:
        return seeds
    expand = expand or "both"
    by_path = {node.path: (node, score, list(matched)) for node, score, matched in seeds}
    frontier = [node.path for node, _, _ in seeds]
    visited = set(frontier)
    for d in range(1, depth + 1):
        next_frontier: List[str] = []
        for rel in frontier:
            node = nodes.get(rel)
            if not node:
                continue
            neighbors: List[str] = []
            if expand in {"outgoing", "both"}:
                neighbors.extend([link.resolved_path for link in node.outgoing_links if link.resolved_path])
            if expand in {"incoming", "both"}:
                neighbors.extend(node.incoming_links)
            for nrel in neighbors:
                if not nrel or nrel in visited or nrel not in nodes:
                    continue
                visited.add(nrel)
                next_frontier.append(nrel)
                by_path[nrel] = (nodes[nrel], max(1, 10 - d), [f"expanded_{expand}:depth{d}"])
        frontier = next_frontier
        if not frontier:
            break
    return sorted(by_path.values(), key=lambda item: (-item[1], item[0].path.lower()))


def _truncate_value(value: Any, max_chars: int) -> Tuple[Any, bool]:
    safe = _json_safe(value)
    text = json.dumps(safe, ensure_ascii=False) if isinstance(safe, (dict, list)) else str(safe)
    if len(text) <= max_chars:
        return safe, False
    return text[: max(0, max_chars - 1)] + "…", True


def _frontmatter_field_order(preset: str, fields: Sequence[str]) -> List[str]:
    if fields:
        return list(dict.fromkeys(str(field) for field in fields if str(field).strip()))
    if preset == "all":
        return []
    return list(DEFAULT_FRONTMATTER_PRESETS.get(preset, DEFAULT_FRONTMATTER_PRESETS["default"]))


def _frontmatter_summary(node: Node, preset: str, fields: Sequence[str], max_value_chars: int) -> Tuple[Dict[str, Any], List[str], List[str], List[str]]:
    selected = _frontmatter_field_order(preset, fields)
    present_keys = sorted(str(key) for key in node.frontmatter.keys())
    field_order = selected or present_keys
    summary: Dict[str, Any] = {}
    truncated: List[str] = []
    for key in field_order:
        if key not in node.frontmatter:
            continue
        value, was_truncated = _truncate_value(node.frontmatter[key], max_value_chars)
        summary[key] = value
        if was_truncated:
            truncated.append(key)
    return summary, field_order, present_keys, truncated


def _link_counts(node: Node) -> Dict[str, int]:
    resolved_count = sum(1 for link in node.outgoing_links if link.resolved)
    ambiguous_count = sum(1 for link in node.outgoing_links if link.ambiguous)
    unresolved_count = sum(1 for link in node.outgoing_links if not link.resolved and not link.ambiguous)
    return {
        "incoming_count": len(node.incoming_links),
        "outgoing_count": len(node.outgoing_links),
        "resolved_count": resolved_count,
        "unresolved_count": unresolved_count,
        "ambiguous_count": ambiguous_count,
    }


def _link_samples(node: Node, limit: int) -> Dict[str, Any]:
    data: Dict[str, Any] = _link_counts(node)
    if limit <= 0:
        return data
    data["incoming_samples"] = node.incoming_links[:limit]
    data["outgoing_samples"] = [link.to_dict() for link in node.outgoing_links[:limit]]
    return data


def _result_json_len(data: Dict[str, Any]) -> int:
    return len(json.dumps(data, ensure_ascii=False, separators=(",", ":")))


def _cap_result(data: Dict[str, Any], max_chars: int) -> Dict[str, Any]:
    if _result_json_len(data) <= max_chars:
        return data
    capped = dict(data)
    capped["result_capped"] = True
    capped.setdefault("truncated_fields", [])
    for result_field in ("why_read", "headings", "frontmatter_keys_present"):
        if result_field in capped and _result_json_len(capped) > max_chars:
            capped.pop(result_field, None)
            capped["truncated_fields"].append(result_field)
    if "links" in capped and _result_json_len(capped) > max_chars:
        links = capped.get("links")
        if isinstance(links, dict):
            capped["links"] = {key: value for key, value in links.items() if key.endswith("_count")}
            capped["truncated_fields"].append("links.samples")
    if "frontmatter_summary" in capped and _result_json_len(capped) > max_chars:
        summary = capped.get("frontmatter_summary")
        if isinstance(summary, dict):
            keep: Dict[str, Any] = {}
            for key, value in summary.items():
                keep[key] = value
                capped["frontmatter_summary"] = keep
                if _result_json_len(capped) > max_chars:
                    keep.pop(key, None)
                    break
            capped["truncated_fields"].append("frontmatter_summary")
    return capped


def _project_node_full(node: Node, score: int, matched: List[str], include: Sequence[str], mode: str = "auto") -> Dict[str, Any]:
    include_set = set(include if include is not None else ["frontmatter", "outgoing_links", "incoming_links"])
    data: Dict[str, Any] = {
        "path": node.path,
        "basename": node.basename,
        "title": node.title,
        "score": score,
        "matched": matched,
        "frontmatter_ok": node.frontmatter_ok,
    }
    if node.frontmatter_error:
        data["frontmatter_error"] = node.frontmatter_error
    if "frontmatter" in include_set:
        data["frontmatter"] = node.frontmatter
    if "headings" in include_set:
        data["headings"] = node.headings[:20]
    if "outgoing_links" in include_set:
        data["outgoing_links"] = [link.to_dict() for link in node.outgoing_links[:MAX_LINKS_PER_NODE]]
    if "incoming_links" in include_set:
        data["incoming_links"] = node.incoming_links[:MAX_LINKS_PER_NODE]
    if "stats" in include_set:
        data["size"] = node.size
        data["mtime"] = node.mtime
    if "why_read" in include_set:
        data["why_read"] = _why_read(node, matched, mode)
    return data


def _project_node_compact(node: Node, score: int, matched: List[str], include: Optional[Sequence[str]], mode: str, output_opts: Dict[str, Any]) -> Dict[str, Any]:
    include_set = set(include if include is not None else ["frontmatter_summary", "link_counts", "why_read"])
    data: Dict[str, Any] = {
        "path": node.path,
        "basename": node.basename,
        "title": node.title,
        "score": score,
        "matched": matched,
        "frontmatter_ok": node.frontmatter_ok,
    }
    if node.frontmatter_error:
        data["frontmatter_error"] = node.frontmatter_error
    if "frontmatter_summary" in include_set:
        summary, fields_used, present, truncated = _frontmatter_summary(
            node,
            output_opts["frontmatter_preset"],
            output_opts["frontmatter_fields"],
            output_opts["max_frontmatter_value_chars"],
        )
        data["frontmatter_summary"] = summary
        data["frontmatter_keys_present"] = present
        if truncated:
            data["truncated_frontmatter_fields"] = truncated
    if "headings" in include_set:
        data["headings"] = node.headings[:20]
    if "stats" in include_set:
        data["size"] = node.size
        data["mtime"] = node.mtime
    if "link_counts" in include_set:
        if output_opts["link_detail"] == "full":
            data["links"] = {
                **_link_counts(node),
                "incoming": node.incoming_links[:MAX_LINKS_PER_NODE],
                "outgoing": [link.to_dict() for link in node.outgoing_links[:MAX_LINKS_PER_NODE]],
            }
        elif output_opts["link_detail"] == "samples":
            data["links"] = _link_samples(node, output_opts["link_sample_limit"])
        else:
            data["links"] = _link_counts(node)
    if output_opts["evidence_detail"] != "none" and "why_read" in include_set:
        data["why_read"] = _why_read(node, matched, mode)
    return _cap_result(data, output_opts["max_chars_per_result"])


def _project_node(node: Node, score: int, matched: List[str], include: Optional[Sequence[str]], mode: str, output_opts: Dict[str, Any]) -> Dict[str, Any]:
    include_set = set(include or [])
    force_legacy = bool(include_set & LEGACY_INCLUDE_FIELDS)
    if output_opts["output_mode"] == "full" or force_legacy:
        return _project_node_full(node, score, matched, include if include is not None else ["frontmatter", "outgoing_links", "incoming_links"], mode)
    return _project_node_compact(node, score, matched, include, mode, output_opts)


def _graph_health_summary(nodes: Dict[str, Node], *, limit: int = 10) -> Dict[str, Any]:
    unresolved_targets: Dict[str, int] = {}
    orphan_count = ambiguous_count = unresolved_node_count = has_outgoing_count = has_incoming_count = 0
    for node in nodes.values():
        if not node.incoming_links:
            orphan_count += 1
        if node.outgoing_links:
            has_outgoing_count += 1
        if node.incoming_links:
            has_incoming_count += 1
        node_has_unresolved = False
        for link in node.outgoing_links:
            if link.ambiguous:
                ambiguous_count += 1
            elif not link.resolved:
                node_has_unresolved = True
                if link.target:
                    unresolved_targets[link.target] = unresolved_targets.get(link.target, 0) + 1
        if node_has_unresolved:
            unresolved_node_count += 1
    top_unresolved = [target for target, _count in sorted(unresolved_targets.items(), key=lambda item: (-item[1], item[0].lower()))[:limit]]
    return {
        "orphan_count": orphan_count,
        "unresolved_node_count": unresolved_node_count,
        "ambiguous_link_count": ambiguous_count,
        "has_outgoing_count": has_outgoing_count,
        "has_incoming_count": has_incoming_count,
        "top_unresolved_targets": top_unresolved,
    }


def _cap_response(results: List[Dict[str, Any]], max_total_chars: int) -> Tuple[List[Dict[str, Any]], bool, int]:
    kept: List[Dict[str, Any]] = []
    for item in results:
        trial = kept + [item]
        if len(json.dumps(trial, ensure_ascii=False, separators=(",", ":"))) > max_total_chars and kept:
            return kept, True, len(results) - len(kept)
        kept.append(item)
    return kept, False, 0


def node_search(
    *,
    scope: str | None = None,
    path_filter: str | None = None,
    exclude_path_filter: Optional[Sequence[str]] = None,
    exclude_defaults: bool = True,
    mode: str = "auto",
    query: str = "",
    query_regex: bool = False,
    where: Optional[Sequence[str]] = None,
    frontmatter: Optional[Dict[str, Any]] = None,
    tags: Optional[Sequence[str]] = None,
    has_links_to: Optional[Sequence[str]] = None,
    linked_from: Optional[Sequence[str]] = None,
    link_state: Optional[Sequence[str]] = None,
    include: Optional[Sequence[str]] = None,
    output_mode: str | None = None,
    frontmatter_preset: str | None = None,
    frontmatter_fields: Optional[Sequence[str]] = None,
    link_detail: str | None = None,
    link_sample_limit: int | None = None,
    evidence_detail: str | None = None,
    max_frontmatter_value_chars: int | None = None,
    max_chars_per_result: int | None = None,
    max_total_chars: int | None = None,
    depth: int = 0,
    expand: str = "both",
    limit: int = 20,
    refresh: bool = False,
    root: Path | None = None,
    cache_path: Path | None = None,
) -> Dict[str, Any]:
    mode = _normalize_mode(mode)
    output_opts = _output_options(
        output_mode=output_mode,
        frontmatter_preset=frontmatter_preset,
        frontmatter_fields=frontmatter_fields,
        link_detail=link_detail,
        link_sample_limit=link_sample_limit,
        evidence_detail=evidence_detail,
        max_frontmatter_value_chars=max_frontmatter_value_chars,
        max_chars_per_result=max_chars_per_result,
        max_total_chars=max_total_chars,
    )
    if limit < 1:
        limit = 1
    limit = min(limit, MAX_RESULTS)
    depth = max(0, min(int(depth or 0), MAX_DEPTH))
    has_narrowing = bool(
        (query or "").strip()
        or mode != "auto"
        or path_filter
        or frontmatter
        or tags
        or has_links_to
        or linked_from
        or link_state
    )
    if not has_narrowing:
        raise NodeSearchError(EMPTY_CALL_GUIDANCE)
    if query_regex and not (query or "").strip():
        raise NodeSearchError("query_regex=true requires a non-empty query")
    if expand not in {"incoming", "outgoing", "both"}:
        raise NodeSearchError("expand must be one of: incoming, outgoing, both")
    cache_path = Path(cache_path or default_cache()).expanduser().resolve()
    index = build_index(scope, root=root, cache_path=cache_path, refresh=refresh)
    nodes: Dict[str, Node] = index["nodes"]
    exclude_terms = list(exclude_path_filter or [])
    if exclude_defaults:
        exclude_terms.extend(DEFAULT_EXCLUDE_PATHS)
    if exclude_terms:
        nodes = {rel: node for rel, node in nodes.items() if not _is_excluded_path(rel, exclude_terms)}
    seed_nodes = nodes
    if path_filter:
        pf = path_filter.lower()
        seed_nodes = {rel: node for rel, node in nodes.items() if pf in rel.lower()}
    filtered = _filter_nodes(
        seed_nodes,
        mode=mode,
        query=query or "",
        where=where or ["path", "basename", "frontmatter", "links", "headings"],
        frontmatter=frontmatter or None,
        tags=tags or [],
        has_links_to=has_links_to or [],
        linked_from=linked_from or [],
        link_state=link_state or [],
        query_regex=query_regex,
    )
    expanded = _expand_results(filtered[:limit], nodes, depth, expand)
    limit_omitted = max(0, len(filtered) - limit)
    truncated = limit_omitted > 0 or len(expanded) > limit
    projected = [_project_node(node, score, matched, include, mode, output_opts) for node, score, matched in expanded[:limit]]
    results, response_capped, cap_omitted = _cap_response(projected, output_opts["max_total_chars"])
    omitted = limit_omitted + cap_omitted
    compact = output_opts["output_mode"] == "compact" and not (set(include or []) & LEGACY_INCLUDE_FIELDS)
    response: Dict[str, Any] = {
        "success": True,
        "root": index["root"],
        "base": index["base"],
        "mode": mode,
        "result_schema_version": RESULT_SCHEMA_COMPACT_V1 if compact else RESULT_SCHEMA_FULL_V1,
        "output_mode": "compact" if compact else "full",
        "exclude_defaults": exclude_defaults,
        "exclude_path_filter": exclude_path_filter or [],
        "query": query,
        "count": len(results),
        "truncated": truncated or response_capped,
        "response_capped": response_capped,
        "stats": index["stats"],
        "results": results,
    }
    if omitted:
        response["omitted_results_count"] = omitted
    if compact:
        response["frontmatter_preset"] = output_opts["frontmatter_preset"]
        response["frontmatter_fields_used"] = _frontmatter_field_order(output_opts["frontmatter_preset"], output_opts["frontmatter_fields"])
        response["link_detail"] = output_opts["link_detail"]
        response["evidence_detail"] = output_opts["evidence_detail"]
    if mode == "graph_health" or link_state:
        response["graph_health_summary"] = _graph_health_summary(seed_nodes)
    return response
