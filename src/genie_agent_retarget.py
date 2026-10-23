"""Retarget Unity Catalog references in Genie Agent (Genie space) definitions.

A serialized Genie space hardcodes fully-qualified names (catalog.schema.table)
in many places: data source identifiers, join specs, SQL snippets, example and
benchmark SQL, and free-form text instructions. Volume data sources and file paths
use the same names as paths (/Volumes/catalog/schema/volume/...). This module
rewrites both with prefix rules, from most to least specific:

    catalog.schema.table -> catalog.schema.table   (exact table or volume)
    catalog.schema       -> catalog.schema         (whole schema)
    catalog              -> catalog                (whole catalog)

A rule and its target must have the same number of parts. The most specific
rule wins, and every name is rewritten once (rules never chain).

Use it as a Python mutator for Declarative Automation Bundles (DABs):

    python:
      venv_path: .venv
      mutators:
        - 'genie_agent_retarget:retarget_genie_spaces'

    variables:
      genie_table_mapping:
        type: complex
        default:
          main.gold: ${var.catalog}.${var.gold_schema}

or from the command line (for example, to normalize a space that was exported
from a non-default environment back to the authoring names):

    python -m genie_agent_retarget space.geniespace.json -m dev_cat.gold=main.gold
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import replace
from typing import Any, Mapping

from databricks.bundles.core import Bundle, Variable, genie_space_mutator
from databricks.bundles.genie_spaces import GenieSpace

__all__ = ["Rules", "parse_rules", "map_name", "retarget_text", "retarget_space",
           "retarget_genie_spaces", "MAPPING_VARIABLE"]

MAPPING_VARIABLE = "genie_table_mapping"
"""Bundle variable (type: complex) holding the {source_prefix: target_prefix} rules."""

# One identifier part: plain (letters, digits, underscore) or backtick-quoted.
_PART = r"(?:`(?:[^`]|``)+`|[A-Za-z_]\w*)"
# Up to 3 dot-separated parts that are not part of a longer dotted chain on the left.
_NAME = re.compile(rf"(?<![\w.`])({_PART})(?:\.({_PART}))?(?:\.({_PART}))?")

# A volume path: /Volumes/<catalog>/<schema>/<volume>, then the end, a "/", or a delimiter.
# Paths have no backticks; a part ends at "/", a quote, a space, or SQL punctuation.
_VPART = r"[^/\s'\"`(),;]+"
_VOLUME = re.compile(rf"(?<![\w/])/Volumes/({_VPART})/({_VPART})/({_VPART})(?=[/\s'\"`),;]|$)")

Rules = dict[tuple[str, ...], tuple[str, ...]]


# A rule key or value: 1 to 3 parts, each backtick-quoted (`my.catalog`) or plain. Plain parts
# may contain any character except dots, backticks and spaces, so `my-catalog` works unquoted.
_RULE_PART = r"(?:`(?:[^`]|``)+`|[^.`\s]+)"
_RULE_NAME = re.compile(rf"\s*({_RULE_PART})(?:\.({_RULE_PART}))?(?:\.({_RULE_PART}))?\s*")


def _split(name: str) -> tuple[str, ...]:
    m = _RULE_NAME.fullmatch(name)
    if not m:
        raise ValueError(f"expected catalog[.schema[.table]], got {name!r}")
    return tuple(_unquote(p) for p in m.groups() if p is not None)


def parse_rules(mapping: Mapping[str, str]) -> Rules:
    """Validate {source_prefix: target_prefix} and normalize it to lower-case part tuples."""
    rules: Rules = {}
    for old, new in (mapping or {}).items():
        src, dst = _split(str(old)), _split(str(new))
        if len(src) != len(dst):
            raise ValueError(f"rule {old!r} -> {new!r}: source and target must have "
                             "the same number of parts")
        # Unity Catalog names are case-insensitive: match lower-case, keep the target as written.
        rules[tuple(p.lower() for p in src)] = dst
    return rules


def map_name(parts: tuple[str, ...], rules: Rules) -> tuple[str, ...] | None:
    """Return the new name for a catalog, schema, or table name (as a tuple of
    parts), or None if no rule changes it."""
    key = tuple(p.lower() for p in parts)
    for n in (3, 2, 1):  # most specific first
        if (target := rules.get(key[:n])) is not None:
            new = target + tuple(parts[n:])
            return None if tuple(p.lower() for p in new) == key else new
    return None


def _unquote(part: str) -> str:
    return part[1:-1].replace("``", "`") if part.startswith("`") else part


def _quote(part: str) -> str:
    return "`" + part.replace("`", "``") + "`"


def retarget_text(text: str, rules: Rules) -> str:
    """Rewrite every 3-part name and every volume path in `text` that a rule
    matches, in a single pass.

    Longer chains such as `catalog.schema.table.column` are handled (the first
    three parts are the table). Quoting is preserved per part: if the source
    part was backtick-quoted, the new part is quoted too. A volume path
    /Volumes/c/s/v/... is matched as the name c.s.v."""
    if not rules or not text:
        return text

    def sub(m: re.Match) -> str:
        raw = m.groups()
        if raw[2] is None:  # fewer than 3 parts: not a table reference
            return m.group(0)
        new = map_name(tuple(_unquote(p) for p in raw), rules)
        if new is None:
            return m.group(0)
        return ".".join(_quote(n) if r.startswith("`") else n for r, n in zip(raw, new))

    def vsub(m: re.Match) -> str:
        new = map_name(m.groups(), rules)
        return m.group(0) if new is None else "/Volumes/" + "/".join(new)

    # Paths use "/" and names use ".", so the two passes never match the same text.
    return _VOLUME.sub(vsub, _NAME.sub(sub, text))


def _walk(node: Any, fn) -> Any:
    if isinstance(node, dict):
        return {k: _walk(v, fn) for k, v in node.items()}
    if isinstance(node, list):
        return [_walk(v, fn) for v in node]
    if isinstance(node, str):
        return fn(node)
    return node


def _short(identifier: str) -> str:
    return _unquote(identifier.rsplit(".", 1)[-1]) if identifier else ""


def retarget_space(space: dict | str, rules: Rules) -> tuple[dict, dict[str, str]]:
    """Rewrite all references in a serialized Genie space.

    Returns (new_space, renamed) where `renamed` maps each changed data source
    identifier to its new value. Accepts the space as a dict or a JSON string."""
    if isinstance(space, str):
        space = json.loads(space)
    if not rules:
        return space, {}

    out = _walk(space, lambda s: retarget_text(s, rules))

    renamed: dict[str, str] = {}
    data_sources = out.get("data_sources") or {}
    for orig, new in zip(_identifiers(space), _identifiers(out)):
        if orig != new:
            renamed[orig] = new

    _rename_short_qualifiers(out, renamed)

    # The Genie API rejects data sources not sorted by identifier, and a rewrite can change the order.
    for key, items in data_sources.items():
        if isinstance(items, list) and all(isinstance(i, dict) and "identifier" in i for i in items):
            items.sort(key=lambda i: i["identifier"])
    # Volumes have a path, not an identifier; the API also wants them sorted.
    volumes = data_sources.get("volumes")
    if isinstance(volumes, list) and all(isinstance(v, dict) and "path" in v for v in volumes):
        volumes.sort(key=lambda v: v["path"])
    return out, renamed


def _rename_short_qualifiers(space: dict, renamed: dict[str, str]) -> None:
    """Follow table renames in SQL that qualifies columns by the short table name.

    Join specs (`authentication.actor.user.uid = ...`) and SQL snippets
    (`oss_iocs.ioc_type = 'URL'`) use the bare table name as a qualifier, so a
    table-level rule that renames a table must rename `old.` to `new.` there too.
    This is done in SQL fields only (not in free text), and only when the old
    short name is unambiguous: no other table in the space has the same short name."""
    shorts = [_short(i).lower() for i in _identifiers(space)]
    renames = {}
    for old_id, new_id in renamed.items():
        old, new = _short(old_id), _short(new_id)
        if old.lower() != new.lower() and old.lower() not in shorts:
            renames[old.lower()] = new
    if not renames:
        return
    pat = re.compile(rf"(?<![\w.`])({'|'.join(map(re.escape, renames))})(?=\.)", re.IGNORECASE)

    def fix(sql: list | None, aliases: frozenset = frozenset()) -> list | None:
        if not isinstance(sql, list):
            return sql
        sub = lambda m: m.group(0) if m.group(1).lower() in aliases else renames[m.group(1).lower()]
        return [pat.sub(sub, s) if isinstance(s, str) else s for s in sql]

    def fix_in(item: dict, key: str, aliases: frozenset = frozenset()) -> None:
        if isinstance(item, dict) and key in item:
            item[key] = fix(item[key], aliases)

    instructions = space.get("instructions") or {}
    for spec in instructions.get("join_specs") or []:
        # an explicit alias equal to the old name is a real alias, not a table name
        aliases = frozenset((spec.get(side) or {}).get("alias", "").lower() for side in ("left", "right"))
        fix_in(spec, "sql", aliases)
    for items in (instructions.get("sql_snippets") or {}).values():
        for item in items if isinstance(items, list) else []:
            fix_in(item, "sql")
    for item in instructions.get("example_question_sqls") or []:
        fix_in(item, "sql")
    for question in (space.get("benchmarks") or {}).get("questions") or []:
        for answer in question.get("answer") or []:
            if answer.get("format", "SQL") == "SQL":
                fix_in(answer, "content")


def _identifiers(space: dict) -> list[str]:
    ids = []
    for items in (space.get("data_sources") or {}).values():
        if isinstance(items, list):
            ids += [i.get("identifier", "") for i in items if isinstance(i, dict)]
    return ids


# ---------------------------------------------------------------------------
# DABs (PyDABs) mutator

def _bundle_rules(bundle: Bundle) -> Rules:
    if MAPPING_VARIABLE not in bundle.variables:
        raise ValueError(f"genie_agent_retarget: define a complex variable '{MAPPING_VARIABLE}' "
                         "in databricks.yml (use {} for no rewriting)")
    value = bundle.resolve_variable(Variable(path=f"var.{MAPPING_VARIABLE}", type=dict[str, str]))
    return parse_rules(value or {})


@genie_space_mutator
def retarget_genie_spaces(bundle: Bundle, space: GenieSpace) -> GenieSpace:
    """Rewrite every Genie space in the bundle with `var.genie_table_mapping`."""
    serialized = space.serialized_space
    if serialized is None:
        return space
    rules = _bundle_rules(bundle)
    original = json.loads(serialized) if isinstance(serialized, str) else serialized
    body, _ = retarget_space(original, rules)
    if body == original:
        return space
    # file_path and an inline serialized_space are mutually exclusive.
    return replace(space, file_path=None, serialized_space=json.dumps(body, indent=2))


# ---------------------------------------------------------------------------
# CLI

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m genie_agent_retarget",
                                description="Rewrite UC references in a serialized Genie space.")
    p.add_argument("file", help="serialized Genie space JSON ('-' for stdin)")
    p.add_argument("-m", "--map", action="append", default=[], metavar="OLD=NEW",
                   help="rewrite rule; catalog, catalog.schema or catalog.schema.table (repeatable)")
    p.add_argument("-o", "--output", help="output file (default: stdout)")
    p.add_argument("--in-place", action="store_true", help="overwrite the input file")
    args = p.parse_args(argv)

    mapping = dict(r.split("=", 1) for r in args.map if "=" in r)
    if len(mapping) != len(args.map):
        p.error("every --map must look like OLD=NEW")
    rules = parse_rules(mapping)
    src = sys.stdin.read() if args.file == "-" else open(args.file, encoding="utf-8").read()
    body, renamed = retarget_space(src, rules)
    for old, new in renamed.items():
        print(f"{old} -> {new}", file=sys.stderr)
    text = json.dumps(body, indent=2, ensure_ascii=False) + "\n"
    target = args.file if args.in_place else args.output
    if target and target != "-":
        with open(target, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
