#!/usr/bin/env python3
"""bibkey - citation key lookup and verification, for humans and agents.

Source of truth is the .bib the document actually compiles against (discovered
from \addbibresource / \bibliography), not the Zotero database. A Better BibTeX
key that is absent from that .bib cannot be cited.

  bibkey find QUERY      search entries, print keys
  bibkey check [TEX]     report cited keys missing from the .bib, plus duplicate keys
                         (exit 2 on a missing key; duplicates only block with --strict)
  bibkey dedup           drop repeated keys in the .bib (--write to apply)
  bibkey prune           drop entries whose key is no longer a live Zotero item
  bibkey dups            list duplicate items in zotero.sqlite (--write-report PATH)
  bibkey merge           merge those duplicates in Zotero through the dedupe plugin
                         (dry run by default; --yes applies, items go to the Trash)
  bibkey bibs [TEX]      show which .bib files are in scope
  bibkey zotero QUERY    search Zotero sqlite, flag keys not present in the .bib

Exit status: 0 ok, 1 no match, 2 check found problems.
"""

import argparse
import json
import re
import shutil
import sqlite3
import sys
import tempfile
import unicodedata
from pathlib import Path
from typing import List, Optional, Tuple

ZOTERO_DB = Path.home() / "Zotero" / "zotero.sqlite"
VERSION = "1.1"
# Directories never scanned when looking for a TeX root: build output, VCS,
# and paperflow's generated copies (which duplicate the real document).
SKIP_DIRS = {"archive", "build", "dist", "node_modules", "__pycache__", "submission"}
DEFAULT_TEX_NAMES = ("main.tex", "paper.tex", "manuscript.tex", "ms.tex")

ADD_BIB_RE = re.compile(r"\\addbibresource(?:\[[^\]]*\])?\{([^}]*)\}")
BIBLIOGRAPHY_RE = re.compile(r"\\bibliography\{([^}]*)\}")
CITE_RE = re.compile(
    r"\\[a-zA-Z]*[Cc]ite[a-zA-Z]*\*?(?:\([^)]*\)){0,2}(?:\[[^\]]*\]){0,2}\{([^}]*)\}"
)
FIELD_RE = re.compile(r"^\s*([a-zA-Z]+)\s*=\s*(.+?),?\s*$")


def plain(text: str) -> str:
    """Reduce a BibTeX value to comparable plain text."""
    text = re.sub(r"\\[a-zA-Z]+\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"[{}\\]", "", text)
    text = "".join(c for c in unicodedata.normalize("NFKD", text)
                   if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text).strip()


def read_balanced(text: str, start: int) -> Tuple[str, int]:
    """Return the body of an entry starting at its opening brace."""
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:index], index
    return text[start + 1:], len(text)


class Entry:
    __slots__ = ("key", "entry_type", "fields")

    def __init__(self, key: str, entry_type: str, fields: dict) -> None:
        self.key = key
        self.entry_type = entry_type
        self.fields = fields

    @property
    def authors(self) -> str:
        return "; ".join(re.split(r"\s+and\s+", self.fields.get("author", "")))

    @property
    def year(self) -> str:
        match = re.search(r"(19|20)\d{2}", self.fields.get("date", self.fields.get("year", "")))
        return match.group(0) if match else ""

    def as_row(self) -> dict:
        return {
            "key": self.key,
            "type": self.entry_type,
            "year": self.year,
            "title": self.fields.get("title", ""),
            "authors": self.authors,
        }


def parse_bib(path: Path) -> List[Entry]:
    """Minimal but brace-safe BibTeX parser."""
    text = path.read_text(encoding="utf-8", errors="replace")
    entries: List[Entry] = []
    index = 0
    while True:
        at = text.find("@", index)
        if at < 0:
            break
        head = re.match(r"@([a-zA-Z]+)\s*\{", text[at:])
        if not head:
            index = at + 1
            continue
        entry_type = head.group(1).lower()
        body, end = read_balanced(text, at + head.end() - 1)
        index = end + 1
        if entry_type in ("preamble", "string"):
            continue
        comma = body.find(",")
        if comma < 0:
            continue
        key = body[:comma].strip()
        fields: dict = {}
        for line in body[comma + 1:].splitlines():
            match = FIELD_RE.match(line)
            if match and match.group(1).lower() != "ids":
                fields.setdefault(match.group(1).lower(), plain(match.group(2)))
        if key:
            entries.append(Entry(key, entry_type, fields))
    return entries


def split_entries(text: str) -> List[Tuple[Optional[str], str]]:
    """Split raw BibTeX into (key, raw) blocks, preserving non-entry blocks.

    @comment / @string / @preamble come back with key None and are never dropped.
    """
    blocks: List[Tuple[Optional[str], str]] = []
    index = 0
    while True:
        at = text.find("@", index)
        if at < 0:
            break
        head = re.match(r"@([a-zA-Z]+)\s*\{", text[at:])
        if not head:
            index = at + 1
            continue
        entry_type = head.group(1).lower()
        body, end = read_balanced(text, at + head.end() - 1)
        raw = text[at:end + 1]
        index = end + 1
        if entry_type in ("preamble", "string", "comment"):
            blocks.append((None, raw))
            continue
        comma = body.find(",")
        key = body[:comma].strip() if comma >= 0 else ""
        blocks.append((key or None, raw))
    return blocks


def raw_field(raw: str, name: str) -> str:
    """Value of a BibTeX field inside a raw entry block, or '' when absent."""
    match = re.search(rf"^\s*{name}\s*=\s*(.+?),?\s*$", raw, re.MULTILINE | re.IGNORECASE)
    return match.group(1).strip().rstrip(",") if match else ""


def doubled_creators(raw: str) -> List[str]:
    """Creator/editor fields that are the same list written twice."""
    suspects = []
    for name in ("author", "editor"):
        value = raw_field(raw, name)
        parts = re.split(r"\s+and\s+", value)
        if len(parts) % 2 == 0:
            half = len(parts) // 2
            if parts[:half] == parts[half:]:
                suspects.append(name)
    return suspects


def dedupe(path: Path, write: bool) -> int:
    """Drop repeated citation keys, keeping the first occurrence."""
    blocks = split_entries(path.read_text(encoding="utf-8", errors="replace"))
    seen: dict = {}
    kept: List[str] = []
    dropped: List[Tuple[str, bool]] = []
    suspects: List[Tuple[str, List[str]]] = []
    for key, raw in blocks:
        if key is None:
            kept.append(raw)
            continue
        if key in seen:
            dropped.append((key, seen[key].strip() == raw.strip()))
            continue
        seen[key] = raw
        kept.append(raw)
        flags = doubled_creators(raw)
        if flags:
            suspects.append((key, flags))

    print(f"{path}: entries {len(blocks)} -> {len(kept)}")
    for key, identical in dropped:
        print(f"DUP      {key}  {'identical' if identical else 'DIFFERENT (check by hand)'}")
    for key, flags in suspects:
        print(f"SUSPECT  {key}  doubled {'/'.join(flags)} field")
    if write:
        path.write_text("\n\n".join(kept) + "\n", encoding="utf-8")
        print(f"written: {path}")
    else:
        print("dry run: nothing written (add --write)")
    return 0


def live_db_keys() -> Optional[set]:
    """Citation keys of items outside the Trash; None if the DB has no citationKey."""
    conn = open_zotero(ZOTERO_DB)
    try:
        field_ids, value = field_getter(conn)
        if "citationKey" not in field_ids:
            return None
        rows = conn.execute(
            """
            SELECT i.itemID
            FROM items i
            WHERE i.itemID NOT IN (SELECT itemID FROM deletedItems)
              AND EXISTS (SELECT 1 FROM itemData d
                           WHERE d.itemID = i.itemID AND d.fieldID = ?)
            """,
            (field_ids["citationKey"],),
        ).fetchall()
        return {value(item_id, "citationKey") for (item_id,) in rows}
    finally:
        conn.close()


def prune(path: Path, write: bool, wanted: Optional[set], cited: set) -> int:
    """Remove entries whose key is not a live Zotero item.

    This is what a Better BibTeX re-export does after a merge, but it also runs
    with the database closed. Cited keys are never removed: if a cited key is
    gone from the database that is a problem to look at, not something to hide.
    """
    blocks = split_entries(path.read_text(encoding="utf-8", errors="replace"))
    kept: List[str] = []
    dropped: List[str] = []
    rescued: List[str] = []
    for key, raw in blocks:
        if key is None:
            kept.append(raw)
            continue
        if wanted is None or key in wanted:
            kept.append(raw)
        elif key in cited:
            kept.append(raw)
            rescued.append(key)
        else:
            dropped.append(key)

    print(f"{path}: entries {len(blocks)} -> {len(kept)}")
    for key in dropped:
        print(f"PRUNE    {key}")
    for key in rescued:
        print(f"KEPT     {key}  (cited, but no live Zotero item - check the database)")
    if write:
        path.write_text("\n\n".join(kept) + "\n", encoding="utf-8")
        print(f"written: {path}")
    else:
        print("dry run: nothing written (add --write)")
    return 0


def discover_tex(start: Optional[Path] = None, depth: int = 4) -> Optional[Path]:
    """Find the TeX root to use when --tex is not given.

    A root is a .tex file with \\documentclass. A file that also names a .bib
    beats one that does not, and the newest wins otherwise. Generated trees
    (.paperflow, build, archive) and submission/ copies are skipped.
    """
    start = (start or Path.cwd()).resolve()
    best: Optional[Tuple[Tuple[int, float], Path]] = None
    for path in start.rglob("*.tex"):
        try:
            rel = path.relative_to(start)
        except ValueError:
            continue
        parts = rel.parts[:-1]
        if len(parts) > depth - 1:
            continue
        if any(part.startswith(".") or part in SKIP_DIRS for part in parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "\\documentclass" not in text:
            continue
        declares_bib = bool(ADD_BIB_RE.search(text) or BIBLIOGRAPHY_RE.search(text))
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        rank = (1 if declares_bib else 0, mtime)
        if best is None or rank > best[0]:
            best = (rank, path)
    return best[1] if best else None


def bib_candidates(tex: Optional[Path]) -> List[Path]:
    """Resolve the .bib files a TeX root pulls in, mirroring LaTeX Workshop."""
    if tex is None:
        # No root: the .bib files in the current directory.
        return sorted(Path.cwd().glob("*.bib"))
    tex = tex.resolve()
    names: List[str] = []
    text = tex.read_text(encoding="utf-8", errors="replace")
    for found in ADD_BIB_RE.findall(text):
        names.extend(part.strip() for part in found.split(","))
    for found in BIBLIOGRAPHY_RE.findall(text):
        names.extend(part.strip() for part in found.split(","))
    resolved: List[Path] = []
    for name in names:
        if not name:
            continue
        if not name.endswith(".bib"):
            name += ".bib"
        for base in (tex.parent, Path.cwd().resolve()):
            candidate = (base / name).resolve()
            if candidate.is_file() and candidate not in resolved:
                resolved.append(candidate)
                break
        else:
            print(f"warning: {name} referenced by {tex} not found", file=sys.stderr)
    return resolved


def union_bibs(texes: List[Path]) -> List[Path]:
    """Every .bib any of these TeX roots compiles against, in first-seen order."""
    seen: set = set()
    merged: List[Path] = []
    for tex in texes:
        for bib in bib_candidates(tex):
            if bib not in seen:
                seen.add(bib)
                merged.append(bib)
    return merged


# ---------------------------------------------------------------- search

def load(bibs: List[Path]) -> Tuple[List[Entry], List[str]]:
    entries: List[Entry] = []
    duplicates: List[str] = []
    for bib in bibs:
        seen_local = set()
        for entry in parse_bib(bib):
            if entry.key in seen_local:
                duplicates.append(f"{entry.key} ({bib.name})")
            seen_local.add(entry.key)
            entries.append(entry)
    return entries, sorted(set(duplicates))


def score(entry: Entry, tokens: List[str]) -> int:
    key = entry.key.lower()
    title = entry.fields.get("title", "").lower()
    authors = entry.authors.lower()
    total = 0
    for token in tokens:
        point = 0
        if token in key:
            point += 5
        if key.startswith(token):
            point += 2
        if token in title:
            point += 3
        if token in authors:
            point += 2
        if token in entry.entry_type:
            point += 1
        total += point
    return total


def find(entries: List[Entry], query: str, limit: int) -> List[Entry]:
    """Rank entries by how many query words they contain.

    Most, not all, words have to appear: requiring every word makes "moire grain
    boundary" (no single entry says all three) come back empty, while requiring
    any one of them makes a stray word like "such" return the whole library.
    """
    tokens = [token for token
              in (raw.lower() for raw in re.split(r"[^\w]+", plain(query))) if len(token) >= 2]
    strong = [token for token in tokens if len(token) >= 3] or tokens
    if not strong:
        return []
    needed = max(1, (len(strong) + 1) // 2)
    scored = [(score(entry, tokens), entry) for entry in entries
              if sum(1 for token in strong if score(entry, [token])) >= needed]
    scored.sort(key=lambda pair: (-pair[0], pair[1].key))
    return [entry for _, entry in scored[:limit]]


# ---------------------------------------------------------------- checks

def cited_keys(tex: Path) -> List[str]:
    """Distinct citation keys used in the TeX root, in first-seen order."""
    text = tex.read_text(encoding="utf-8", errors="replace")
    keys: List[str] = []
    seen: set = set()
    for group in CITE_RE.findall(text):
        for part in re.split(r"[,\s]+", group):
            if part and part not in seen:
                seen.add(part)
                keys.append(part)
    return keys


def check(tex: Path, bibs: List[Path], strict: bool = False) -> int:
    entries, duplicates = load(bibs)
    available = {entry.key for entry in entries}
    used = cited_keys(tex)
    missing = sorted({key for key in used if key not in available})
    unused = sorted(available - set(used))
    print(f"tex:  {tex}")
    print(f"bibs: {', '.join(str(bib) for bib in bibs) or 'NONE'}")
    print(f"cited: {len(used)} keys, in .bib: {len(available)}, uncited: {len(unused)}")
    for key in missing:
        print(f"MISSING  {key}")
    for item in duplicates:
        print(f"DUPLICATE {item}")
    if not missing and not duplicates:
        print("ok")
    if missing:
        return 2
    # Duplicate keys are a library-hygiene problem, not a broken build: BBT puts
    # them back on the next auto-export, so they only block when asked for.
    return 2 if strict and duplicates else 0


# ---------------------------------------------------------------- zotero

def field_getter(conn: sqlite3.Connection) -> Tuple[dict, "object"]:
    """fieldID map plus a per-item field reader for zotero.sqlite."""
    field_ids = dict(conn.execute(
        "SELECT fieldName, fieldID FROM fields "
        "WHERE fieldName IN ('citationKey','title','publicationTitle','date','DOI','url')"
    ).fetchall())

    def value(item_id: int, name: str) -> str:
        field_id = field_ids.get(name, -1)
        row = conn.execute(
            "SELECT qv.value FROM itemData q JOIN itemDataValues qv "
            "ON q.valueID = qv.valueID "
            "WHERE q.itemID = ? AND q.fieldID = ? LIMIT 1",
            (item_id, field_id),
        ).fetchone()
        return row[0] if row and row[0] else ""

    return field_ids, value


def zotero_duplicates(db: Path) -> List[List[dict]]:
    """Items in zotero.sqlite that are the same work, grouped by DOI else title."""
    conn = open_zotero(db)
    try:
        field_ids, value = field_getter(conn)
        if "citationKey" not in field_ids:
            print("warning: no citationKey field; is Better BibTeX installed?", file=sys.stderr)
            return []
        rows = conn.execute(
            """
            SELECT i.itemID, i.key
            FROM items i
            JOIN itemTypes it ON it.itemTypeID = i.itemTypeID
            WHERE it.typeName NOT IN ('attachment', 'note', 'annotation')
              AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
              AND EXISTS (SELECT 1 FROM itemData d
                           WHERE d.itemID = i.itemID AND d.fieldID = ?)
            """,
            (field_ids["citationKey"],),
        ).fetchall()

        def norm(text: str) -> str:
            text = re.sub(r"^https?://(dx\.)?doi\.org/", "", text.strip().lower())
            return re.sub(r"[^a-z0-9]", "", text)

        # An item can match on DOI and on title; an item with no DOI only has its
        # title. Union those signatures so "same DOI" and "same title" chains merge
        # into one group instead of splitting in two.
        parents: dict = {}

        def root_of(node):
            while parents.setdefault(node, node) != node:
                parents[node] = parents[parents[node]]
                node = parents[node]
            return node

        def union(left, right):
            left_root, right_root = root_of(left), root_of(right)
            if left_root != right_root:
                parents[right_root] = left_root

        by_signature: dict = {}
        records: dict = {}
        for item_id, item_key in rows:
            record = {
                "citation_key": value(item_id, "citationKey"),
                "item_key": item_key,
                "title": value(item_id, "title"),
                "doi": value(item_id, "DOI"),
                "date": value(item_id, "date"),
            }
            records[item_id] = record
            root_of(item_id)
            for signature in (norm(record["doi"]), norm(record["title"])):
                if not signature:
                    continue
                if signature in by_signature:
                    union(by_signature[signature], item_id)
                else:
                    by_signature[signature] = item_id

        groups: dict = {}
        for item_id, record in records.items():
            groups.setdefault(root_of(item_id), []).append(record)
        return [g for g in groups.values() if len(g) > 1]
    finally:
        conn.close()


def keep_rank(record: dict, cited: set, available: set) -> Tuple[int, int, int, str]:
    """Sort key for the copy to keep: cited > no letter suffix > in the .bib > name."""
    key = record["citation_key"]
    return (key not in cited, bool(re.search(r"[a-z]$", key)),
            key not in available, key)


def merge_plan(groups: List[List[dict]], cited: set, available: set) -> List[Tuple[dict, List[dict]]]:
    plan = []
    for group in sorted(groups, key=lambda g: min(r["citation_key"] for r in g)):
        keep = min(group, key=lambda r: keep_rank(r, cited, available))
        plan.append((keep, [r for r in group if r is not keep]))
    return plan


def dups(bibs: List[Path], tex: Optional[Path], report: Optional[Path],
         as_json: bool = False) -> int:
    """Print (and optionally save) a merge list for the Zotero database."""
    groups = zotero_duplicates(ZOTERO_DB)
    available = {entry.key for entry in load(bibs)[0]}
    cited = set(cited_keys(tex)) if tex else set()
    redundant = sum(len(group) - 1 for group in groups)

    if not as_json:
        print(f"bibs: {', '.join(str(bib) for bib in bibs)}")
        if tex:
            print(f"cited keys from: {tex} ({len(cited)} keys)")
        else:
            print("no --tex given: KEEP will not prefer the copy the document cites")

    if as_json:
        payload = []
        for group in groups:
            payload.append([{**record,
                             "in_bib": record["citation_key"] in available,
                             "cited": record["citation_key"] in cited}
                            for record in group])
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    def preferred(record: dict) -> Tuple[int, int, int, str]:
        return keep_rank(record, cited, available)

    header = [
        "# Zotero duplicate items - merge list",
        "",
        "Generated by `bibkey.py dups` (groups zotero.sqlite items by DOI, else title).",
        "Regenerate with:",
        "",
        "```bash",
        f"cd {Path(__file__).resolve().parent.parent}",
        f"bibkey dups --tex {tex or '<root>.tex'} "
        f"--write-report {report or '<path>'}",
        "```",
        "",
        f"- duplicate groups: {len(groups)}",
        f"- items in groups: {sum(len(g) for g in groups)}",
        f"- redundant items: {redundant}",
        "",
        "Better BibTeX exports every duplicate item under its own `a`/`b`/`c` suffixed",
        "key, so the .bib carries those redundant entries too. Those suffixes are also",
        "how keys drift: merging strips a suffix, and a key copied out of the database",
        "stops matching the .bib. Apply these with `bibkey.py merge` (dry run by",
        "default), then run `bibkey.py check`.",
        "",
        "**KEEP** = merge the rest into this item, chosen by: cited in the document >",
        "no letter suffix > present in the .bib > alphabetical.",
        "",
        "| # | KEEP | merge away | title |",
        "|---|------|-----------|-------|",
    ]
    lines = list(header)
    for number, group in enumerate(sorted(groups, key=lambda g: g[0]["citation_key"]), 1):
        keep = min(group, key=preferred)
        rest = [r["citation_key"] for r in group if r is not keep]
        note = " **(cited)**" if keep["citation_key"] in cited else ""
        title = re.sub(r"\s+", " ", keep["title"])[:78]
        lines.append(f"| {number} | `{keep['citation_key']}`{note} | "
                     f"{', '.join(f'`{r}`' for r in rest)} | {title} |")
    text = "\n".join(lines) + "\n"
    if report:
        report.write_text(text, encoding="utf-8")
        print(f"wrote {report}: {len(groups)} groups, {redundant} redundant items")
    else:
        print("\n".join(header[1:]))
        print(f"({len(groups)} groups; use --write-report PATH for the merge list)")
        for number, group in enumerate(sorted(groups, key=lambda g: g[0]["citation_key"])[:10], 1):
            keys = [g["citation_key"] for g in group]
            print(f"  {number}. {' | '.join(keys)}  {group[0]['title'][:60]}")
    return 0


MERGE_URL = "http://127.0.0.1:23119/dedupe/merge"
MERGE_TOKEN_FILE = Path.home() / "Zotero" / "zotero-dedupe-merge-token"


def merge(texes: List[Path], bibs: List[Path], yes: bool, probe: bool,
         plan_only: bool, as_json: bool) -> int:
    """Drive /dedupe/merge from the same grouping `dups` reports.

    The writing half lives in the Zotero plugin, because merging items means
    moving attachments, notes, tags and collections - only Zotero's own merge
    code knows how. This side decides which copy survives.
    """
    import urllib.error
    import urllib.request

    def post(payload: dict) -> dict:
        token = ""
        if MERGE_TOKEN_FILE.exists():
            token = MERGE_TOKEN_FILE.read_text(encoding="utf-8").strip()
        request = urllib.request.Request(
            MERGE_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "X-Zotero-Dedupe-Merge-Token": token},
        )
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")[:400]
            if error.code == 404:
                raise SystemExit(
                    f"{MERGE_URL} does not exist: the installed zotero-dedupe "
                    "plugin has no merge endpoint.\nInstall "
                    "/home/tim/apps/zotero-dedupe-plugin/zotero-dedupe.xpi "
                    "(Tools -> Plugins -> Install Plugin From File) and restart Zotero."
                )
            raise SystemExit(f"HTTP {error.code} from {MERGE_URL}: {body}")
        except urllib.error.URLError as error:
            raise SystemExit(
                f"cannot reach {MERGE_URL}: {error.reason}\n"
                "Is Zotero running, and is zotero-dedupe 1.2.0 installed?"
            )

    if probe:
        print(json.dumps(post({"probe": True}), indent=2, ensure_ascii=False))
        return 0

    groups = zotero_duplicates(ZOTERO_DB)
    available = {entry.key for entry in load(bibs)[0]}
    cited: set = set()
    for tex in texes:
        cited |= set(cited_keys(tex))
    plan = merge_plan(groups, cited, available)

    print("cited by:", ", ".join(str(tex) for tex in texes)
          or "(no --tex: KEEP will not prefer cited copies)")
    print(f"groups: {len(plan)}   items to merge away: {sum(len(r) for _, r in plan)}")
    for keep, rest in plan:
        note = " (cited)" if keep["citation_key"] in cited else ""
        print(f"  {keep['citation_key']}{note}  <- {', '.join(r['citation_key'] for r in rest)}")
    if plan_only:
        print("\n(--plan only: nothing was sent to Zotero)")
        return 0

    payload_groups = [{"keep": keep["item_key"],
                       "merge": [record["item_key"] for record in rest]}
                      for keep, rest in plan]
    done, skipped, items_away = 0, 0, 0
    fills: dict = {}
    type_warnings, notes = [], []
    for start in range(0, len(payload_groups), 25):
        chunk = payload_groups[start:start + 25]
        reply = post({"groups": chunk, "dryRun": not yes})
        done += reply.get("merged" if yes else "planned", 0)
        skipped += reply.get("skipped", 0)
        what = "merged" if yes else "would merge"
        print(f"  {start + 1}-{start + len(chunk)}: {what} "
              f"{reply.get('merged' if yes else 'planned', 0)}, "
              f"skipped {reply.get('skipped', 0)}")
        if as_json:
            print(json.dumps(reply, ensure_ascii=False))
        for result in reply.get("results", []):
            items_away += len(result.get("wouldMerge") or result.get("merged") or [])
            for step in result.get("fieldsToFill", []):
                fills[step["field"]] = fills.get(step["field"], 0) + 1
            if result.get("error"):
                notes.append(f"error: {result['error']}")
            if result.get("why"):
                notes.append(f"skipped {result['keep']['key'] if result.get('keep') else '?'}: "
                             f"{result['why']}")
            for record in result.get("merge", []):
                if record.get("why"):
                    notes.append(f"skipped {record.get('key')}: {record['why']}")
                if record.get("typeMismatch"):
                    type_warnings.append(
                        f"{result.get('keep', {}).get('key')}: merging a "
                        f"{record['itemType']} copy into a "
                        f"{result.get('keep', {}).get('itemType')}"
                    )
    for line in (type_warnings[:10] + notes[:20]):
        print("    " + line)
    if len(type_warnings) > 10:
        print(f"    (+{len(type_warnings) - 10} more type warnings)")
    if fills:
        top = ", ".join(f"{name} ×{count}" for name, count in
                        sorted(fills.items(), key=lambda kv: -kv[1])[:8])
        print(f"fields that {'were' if yes else 'would be'} filled from duplicates: {top}")
    if yes:
        print(f"total {done} groups merged ({items_away} items moved to the Trash), "
              f"{skipped} skipped")
        print("now: re-export Better BibTeX, then `bibkey.py check`")
    else:
        print(f"total {done} groups ready ({items_away} items would move to the Trash), "
              f"{skipped} skipped")
        print("\ndry run only: add --yes to apply. The merged copies go to the Zotero "
              "Trash, so this is undoable until you empty it.")
    return 0 if skipped == 0 else 2


def open_zotero(db: Path) -> sqlite3.Connection:
    """Open zotero.sqlite read-only, falling back to a snapshot copy.

    Zotero holds a write lock while it runs, so a read-only connection can open
    fine and still fail on the first query; probe before deciding.
    """
    conn = None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1)
        conn.execute("SELECT count(*) FROM fields").fetchone()
        return conn
    except sqlite3.OperationalError:
        if conn is not None:
            conn.close()
    staging = Path(tempfile.mkdtemp(prefix="bibkey-"))
    for suffix in ("", "-wal", "-shm"):
        source = db.parent / (db.name + suffix)
        if source.exists():
            shutil.copy2(source, staging / (db.name + suffix))
    return sqlite3.connect(f"file:{staging / db.name}?mode=ro", uri=True)


def zotero_rows(db: Path, query: str, limit: int) -> List[dict]:
    conn = open_zotero(db)
    try:
        field_ids, value = field_getter(conn)
        if "citationKey" not in field_ids:
            print("warning: no citationKey field; is Better BibTeX installed?", file=sys.stderr)
            return []
        tokens = [token for token in re.split(r"[^\w]+", query) if token]
        title_hits, key_hits = [], []
        for token in tokens:
            title_hits.append(
                "EXISTS (SELECT 1 FROM itemData q JOIN itemDataValues qv "
                "ON q.valueID = qv.valueID WHERE q.itemID = i.itemID "
                f"AND q.fieldID = {field_ids['title']} AND qv.value LIKE ?)"
            )
            key_hits.append("ckv.value LIKE ?")
        params = [f"%{token}%" for token in tokens] * 2
        where = f"(({' AND '.join(title_hits)}) OR ({' AND '.join(key_hits)}))"
        rows = conn.execute(
            f"""
            SELECT i.itemID, i.key, ckv.value
            FROM items i
            JOIN itemData ck ON ck.itemID = i.itemID AND ck.fieldID = ?
            JOIN itemDataValues ckv ON ckv.valueID = ck.valueID
            WHERE {where}
            ORDER BY i.dateModified DESC
            LIMIT ?
            """,
            [field_ids["citationKey"], *params, limit],
        ).fetchall()

        return [
            {"citation_key": key, "item_key": item,
             "title": value(item_id, "title"),
             "publication": value(item_id, "publicationTitle"),
             "date": value(item_id, "date")}
            for item_id, item, key in rows
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------- cli

def main(argv: Optional[List[str]] = None) -> int:
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--tex", type=Path, action="append", dest="texes",
                        help="TeX root used to locate the .bib files (repeatable)")
    shared.add_argument("--bib", type=Path, action="append", help="explicit .bib (repeatable)")
    shared.add_argument("--limit", type=int, default=10)
    shared.add_argument("--json", action="store_true", help="machine readable output")

    parser = argparse.ArgumentParser(prog="bibkey", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version=f"bibkey {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("find", "search the .bib in scope and print keys"),
        ("check", "report cited keys missing from the .bib, and duplicate keys"),
        ("dedup", "remove duplicate keys from the .bib (add --write to apply)"),
        ("prune", "remove entries whose key is no longer a live Zotero item (--write)"),
        ("dups", "list duplicate items in zotero.sqlite (merge list)"),
        ("merge", "merge those duplicates in Zotero via the plugin (dry run unless --yes)"),
        ("bibs", "print the .bib files in scope"),
        ("zotero", "search zotero.sqlite (Better BibTeX citationKey)"),
    ):
        sub.add_parser(name, parents=[shared], help=help_text)
    sub.choices["dedup"].add_argument("--write", action="store_true", help="rewrite the .bib")
    sub.choices["prune"].add_argument("--write", action="store_true", help="rewrite the .bib")
    sub.choices["prune"].add_argument("--keys-file", type=Path, metavar="PATH",
                                      help="keep only keys listed in PATH, instead of "
                                           "asking the Zotero database")
    sub.choices["dups"].add_argument("--write-report", type=Path, metavar="PATH",
                                     help="write the merge list to PATH (markdown)")
    for flag, help_text in (
        ("--yes", "apply the merge (default is a dry run)"),
        ("--probe", "ask the plugin what merge implementation it has, then exit"),
        ("--plan", "print the plan without contacting the plugin"),
    ):
        sub.choices["merge"].add_argument(flag, action="store_true", help=help_text)
    sub.choices["check"].add_argument("--strict", action="store_true",
                                      help="also block on duplicate keys")
    for name in ("find", "zotero"):
        sub.choices[name].add_argument(
            "query", nargs="+",
            help="words to look for; order does not matter (quote phrases if you like)")
    args = parser.parse_args(argv)

    texes = args.texes or []
    if not texes:
        # No --tex: use the document in (or under) the current directory.
        root = discover_tex()
        if root is not None:
            texes = [root]
    tex = texes[-1] if texes else None
    bibs = args.bib or bib_candidates(tex)

    if args.command == "dups":
        return dups(bibs, tex, args.write_report, args.json)

    if args.command == "merge":
        if not args.bib:
            # A key can be kept for either document, so "in the .bib" has to mean
            # in any .bib that any of these documents compiles against.
            bibs = union_bibs(texes) if texes else bib_candidates(None)
        return merge(texes, bibs, args.yes, args.probe, args.plan, args.json)

    if args.command == "dedup":
        for bib in bibs:
            dedupe(bib, args.write)
        return 0

    if args.command == "prune":
        if args.keys_file:
            wanted = {line.strip() for line in
                      args.keys_file.read_text(encoding="utf-8").splitlines()
                      if line.strip() and not line.startswith("#")}
        else:
            wanted = live_db_keys()
            if wanted is None:
                print("no citationKey field in the database; pass --keys-file",
                      file=sys.stderr)
                return 2
        cited: set = set()
        for candidate in texes:
            if candidate.exists():
                cited |= set(cited_keys(candidate))
        if not args.bib:
            bibs = union_bibs(texes) if texes else bib_candidates(None)
        if not bibs:
            print("bibkey: no .bib found here; pass --tex or --bib", file=sys.stderr)
            return 2
        for bib in bibs:
            prune(bib, args.write, wanted, cited)
        return 0

    if args.command == "bibs":
        for bib in bibs:
            print(bib)
        return 0 if bibs else 1

    if args.command == "check":
        if tex is None:
            print("bibkey: no TeX root found (no .tex with \\documentclass here); "
                  "pass --tex path/to/main.tex", file=sys.stderr)
            return 2
        return check(tex, bibs, strict=args.strict)

    if args.command == "zotero":
        rows = zotero_rows(ZOTERO_DB, " ".join(args.query), args.limit)
        available = {entry.key for entry in load(bibs)[0]}
        for row in rows:
            row["in_bib"] = row["citation_key"] in available
        if args.json:
            print(json.dumps(rows, ensure_ascii=False))
        else:
            for row in rows:
                flag = "" if row["in_bib"] else "  <- NOT IN .BIB"
                print(f"{row['citation_key']}\t{row['date'][:4]}\t{row['title']}{flag}")
        return 0 if rows else 1

    entries, _ = load(bibs)
    hits = find(entries, " ".join(args.query), args.limit)
    if args.json:
        print(json.dumps([entry.as_row() for entry in hits], ensure_ascii=False))
    else:
        for entry in hits:
            print(f"{entry.key}\t{entry.year}\t{entry.fields.get('title', '')}")
    return 0 if hits else 1


if __name__ == "__main__":
    sys.exit(main())

