"""Scores news stories against severity_keywords.txt.

The keyword file defines the word lists and the rules for combining them (see
the comments at the top of that file). If severity_keywords.txt does not
exist, severity_keywords.example.txt is used instead.

Levels, lowest to highest: usual, notable, severe.
"""

import re
import sys
from pathlib import Path

LEVELS = ("usual", "notable", "severe")
_RANK = {name: i for i, name in enumerate(LEVELS)}


def _norm(text: str) -> str:
    """Lowercase, and treat hyphens, underscores and slashes as spaces."""
    return re.sub(r"\s+", " ", re.sub(r"[-_/]+", " ", text.lower())).strip()


def _term_matcher(line: str):
    """Turn one keyword line into a function(raw_text, normalized_text) that
    returns the matched text, or None when the term is not present.

    plain term  -> matches at the start of a word and may keep going
    =term       -> matches the whole word only
    re:pattern  -> regular expression
    """
    if line.lower().startswith("re:"):
        rx = re.compile(line[3:].strip(), re.IGNORECASE)

        def match(raw, norm, rx=rx):
            m = rx.search(raw)
            return m.group(0).strip() if m else None

        return match

    exact = line.startswith("=")
    label = (line[1:] if exact else line).strip()
    body = re.escape(_norm(label)).replace(r"\ ", r"\s+")
    rx = re.compile(r"\b" + body + (r"\b" if exact else ""))

    def match(raw, norm, rx=rx, label=label):
        return label if rx.search(norm) else None

    return match


def _parse_rule(line: str, source: str, number: int):
    """'severe: tier2 + watch1'  ->  ('severe', [(1, 'tier2'), (1, 'watch1')])"""
    try:
        level, expr = [x.strip() for x in line.split("#")[0].split(":", 1)]
        level = level.lower()
        if level not in ("severe", "notable"):
            raise ValueError(f"unknown level '{level}'")
        parts = []
        for part in expr.split("+"):
            m = re.fullmatch(r"(?:(\d+)\s*x\s*)?(\w+)", part.strip())
            if not m:
                raise ValueError(f"cannot read '{part.strip()}'")
            parts.append((int(m.group(1) or 1), m.group(2).lower()))
        return level, parts
    except ValueError as exc:
        print(f"[warn] {source} line {number}: bad rule ({exc})", file=sys.stderr)
        return None


def load(base: Path):
    """Read the keyword file. Returns None when there is no keyword file."""
    path = base / "severity_keywords.txt"
    if not path.exists():
        path = base / "severity_keywords.example.txt"
    if not path.exists():
        return None

    lists: dict[str, list] = {}
    rules = []
    current = None
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.fullmatch(r"\[(\w+)\]", line)
        if m:
            current = m.group(1).lower()
            if current != "rules":
                lists.setdefault(current, [])
            continue
        if current is None:
            continue
        if current == "rules":
            rule = _parse_rule(line, path.name, number)
            if rule:
                rules.append(rule)
            continue
        try:
            lists[current].append(_term_matcher(line))
        except re.error as exc:
            print(f"[warn] {path.name} line {number}: bad pattern ({exc})", file=sys.stderr)

    good_rules = []
    for level, parts in rules:
        unknown = [name for _, name in parts if name not in lists]
        if unknown:
            print(f"[warn] {path.name}: a {level} rule uses unknown list "
                  f"'{unknown[0]}'; rule skipped", file=sys.stderr)
        else:
            good_rules.append((level, parts))
    return {"lists": lists, "rules": good_rules, "file": path.name}


def score(keywords: dict, text: str):
    """Returns (level, why) where why is the list of matched terms that made
    the story reach that level."""
    raw = text.lower()
    norm = _norm(text)
    found: dict[str, list[str]] = {}
    for name, matchers in keywords["lists"].items():
        hits: list[str] = []
        for match in matchers:
            hit = match(raw, norm)
            if hit and hit.lower() not in (h.lower() for h in hits):
                hits.append(hit)
        found[name] = hits

    best, why = "usual", []
    for level, parts in keywords["rules"]:
        used: list[str] = []
        if all(len(found[name]) >= need for need, name in parts):
            for need, name in parts:
                used += found[name][:need]
            if _RANK[level] > _RANK[best]:
                best, why = level, used
    return best, why


def score_items(keywords: dict, items: list[dict]):
    """Scores every story. Adds 'level' and 'why' to each item.

    Returns (digest_level, top_item). The digest is as severe as its most
    severe story. top_item is the story that best explains that level, or
    None when everything is business as usual.
    """
    digest, top, top_key = "usual", None, (-1, -1)
    for item in items:
        text = item.get("score_text") or f"{item['title']} {item['blurb']}"
        level, why = score(keywords, text)
        item["level"], item["why"] = level, why
        key = (_RANK[level], len(why))
        if key > top_key:
            top, top_key = item, key
        if _RANK[level] > _RANK[digest]:
            digest = level
    return digest, (top if digest != "usual" else None)
