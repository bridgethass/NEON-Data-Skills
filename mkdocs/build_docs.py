"""Generate the MkDocs docs tree for the NEON-Data-Skills tutorials.

Reads (never modifies) tutorials/**/*.md, normalizes their YAML front matter
using the vocabularies in mkdocs/vocab/, merges curated metadata from
mkdocs/tutorial_meta.csv, and writes a self-contained docs tree to mkdocs/docs/
(gitignored) along with facet browse pages, a JSON catalog for the filter page,
and the site navigation (docs/.nav.yml, loaded by mkdocs/hooks.py).

Usage (from the repo root):
    python mkdocs/build_docs.py                    # build docs/ + print report
    python mkdocs/build_docs.py --init-meta        # add new tutorials to tutorial_meta.csv with guessed values
    python mkdocs/build_docs.py --refresh-products # re-download data product names/systems from the NEON API
"""

from __future__ import annotations

import argparse
import base64
import csv
import fnmatch
import html
import json
import re
import shutil
import subprocess
import sys
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
TUT = REPO / "tutorials"
OUT = HERE / "docs"
PAGES = HERE / "pages"
VOCAB = HERE / "vocab"
META_CSV = HERE / "tutorial_meta.csv"

NEON_TUTORIAL_URL = "https://www.neonscience.org/resources/learning-hub/tutorials/"
GITHUB_BLOB = "https://github.com/NEONScience/NEON-Data-Skills/blob/main/tutorials/"
NEON_API_PRODUCTS = "https://data.neonscience.org/api/v0/products"

META_FIELDS = [
    "path", "sync_id", "title", "url_title", "theme", "subtheme",
    "task_types", "skill_level", "system_override", "reviewed", "notes",
]

SYSTEM_ICONS = {"AOP": ":material-airplane:", "IS": ":material-transmission-tower:", "OS": ":material-leaf:"}
# display labels for top-level tutorial folders in the navigation
NAV_LABELS = {"Tabbed": "R & Python", "GEE": "Google Earth Engine"}

FM_RE = re.compile(r"\A﻿?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n", re.S)
DP_RE = re.compile(r"DP([1-4])\.(\d{5})(?:\.(\d{3}))?")

# --------------------------------------------------------------------------- vocab


def load_yaml(path: Path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def norm_key(s: str) -> str:
    """Normalize a free-text value for vocabulary matching."""
    s = str(s).strip().strip("\"'").strip().lower()
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"[()]", "", s)
    return s


@dataclass
class Vocab:
    topics: dict  # slug -> name
    topic_alias: dict  # norm -> slug
    topic_drop: set
    languages: list  # canonical names in display order
    lang_alias: dict  # lower -> name
    lang_drop: set
    folder_langs: dict
    task_types: dict  # slug -> name
    task_keywords: dict  # slug -> [kw]
    skill_levels: dict  # slug -> name
    systems: dict  # slug -> name
    products: dict  # "DP1.10098.001" or "DP1.10098" -> {name, team, system}
    exclude: list


def load_vocab() -> Vocab:
    t = load_yaml(VOCAB / "topics.yml")
    topics, topic_alias = {}, {}
    for item in t["topics"]:
        topics[item["slug"]] = item["name"]
        topic_alias[norm_key(item["slug"])] = item["slug"]
        topic_alias[norm_key(item["name"])] = item["slug"]
        for a in item.get("aliases", []) or []:
            topic_alias[norm_key(a)] = item["slug"]

    lg = load_yaml(VOCAB / "languages.yml")
    languages, lang_alias = [], {}
    for item in lg["languages"]:
        languages.append(item["name"])
        lang_alias[item["name"].lower()] = item["name"]
        for a in item.get("aliases", []) or []:
            lang_alias[a.lower()] = item["name"]

    tt = load_yaml(VOCAB / "task_types.yml")["task_types"]
    sl = load_yaml(VOCAB / "skill_levels.yml")["skill_levels"]
    sy = load_yaml(VOCAB / "systems.yml")["systems"]

    products = {}
    pfile = VOCAB / "data_products.yml"
    if pfile.exists():
        for p in load_yaml(pfile).get("products", []) or []:
            products[p["code"]] = p

    return Vocab(
        topics=topics,
        topic_alias=topic_alias,
        topic_drop={norm_key(x) for x in t.get("drop", []) or []},
        languages=languages,
        lang_alias=lang_alias,
        lang_drop={x.lower() for x in lg.get("drop", []) or []},
        folder_langs=lg.get("folder_defaults", {}) or {},
        task_types={x["slug"]: x["name"] for x in tt},
        task_keywords={x["slug"]: x.get("keywords", []) for x in tt},
        skill_levels={x["slug"]: x["name"] for x in sl},
        systems={x["slug"]: x["name"] for x in sy},
        products=products,
        exclude=load_yaml(VOCAB / "exclude.yml").get("exclude", []),
    )


# --------------------------------------------------------------------------- parsing


@dataclass
class Tutorial:
    path: str  # posix path relative to tutorials/
    src: Path
    fm: dict
    body: str
    fm_ok: bool
    title: str = ""
    description: str = ""
    sync_id: str = ""
    url_title: str = ""
    languages: list = field(default_factory=list)
    topics: list = field(default_factory=list)
    products: list = field(default_factory=list)  # list of dicts
    system: str = "General"
    derived_system: str = "General"
    task_types: list = field(default_factory=list)
    skill_level: str = ""
    theme: str = ""
    subtheme: str = ""
    series: str = ""
    source_kind: str = "md"  # rmd | ipynb | md
    source_path: str = ""

    @property
    def page_url(self) -> str:
        """Site-relative URL with use_directory_urls (no leading slash)."""
        p = PurePosixPath(self.path)
        if p.name == "index.md":
            return str(p.parent) + "/"
        return str(p.with_suffix("")) + "/"


def as_list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        out = []
        for x in v:
            out.extend(as_list(x))
        return out
    s = str(v).strip()
    if not s or s.lower() in ("null", "na", "none"):
        return []
    return [x.strip() for x in s.split(",") if x.strip()]


def first(fm: dict, *keys):
    for k in keys:
        if fm.get(k) not in (None, ""):
            return fm[k]
    return None


def parse_md(path: Path) -> tuple[dict, str, bool]:
    text = path.read_text(encoding="utf-8", errors="replace")
    m = FM_RE.match(text)
    if not m:
        return {}, text, False
    raw = m.group(1).replace("\t", "  ")
    try:
        fm = yaml.safe_load(raw) or {}
        ok = isinstance(fm, dict)
    except yaml.YAMLError:
        fm, ok = {}, False
    return (fm if ok else {}), text[m.end():], ok


def is_excluded(rel: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel, p) for p in patterns)


def discover(vocab: Vocab) -> list[Tutorial]:
    tuts = []
    for p in sorted(TUT.rglob("*.md")):
        rel = p.relative_to(TUT).as_posix()
        if is_excluded(rel, vocab.exclude):
            continue
        fm, body, ok = parse_md(p)
        tuts.append(Tutorial(path=rel, src=p, fm=fm, body=body, fm_ok=ok))
    return tuts


# --------------------------------------------------------------------------- normalize


class Report:
    def __init__(self):
        self.bad_fm: list[str] = []
        self.unknown_topics: Counter = Counter()
        self.unknown_langs: Counter = Counter()
        self.unknown_products: Counter = Counter()
        self.no_revision: Counter = Counter()
        self.stale: list[str] = []


def normalize(t: Tutorial, vocab: Vocab, rep: Report) -> None:
    fm = t.fm
    if not t.fm_ok:
        rep.bad_fm.append(t.path)

    title = first(fm, "title")
    if not title:
        m = re.search(r"^#\s+(.+)$", t.body, re.M)
        title = m.group(1).strip() if m else PurePosixPath(t.path).stem.replace("-", " ")
    t.title = str(title).strip()
    t.description = str(first(fm, "description") or "").strip()
    t.sync_id = str(first(fm, "syncID") or "").strip()
    t.url_title = str(first(fm, "urlTitle") or "").strip()
    series = as_list(fm.get("tutorialSeries"))
    t.series = series[0] if series else ""

    # Languages / tools
    langs = []
    for raw in as_list(first(fm, "languagesTool", "languageTool", "languageTools", "language")):
        key = raw.lower()
        if key in vocab.lang_drop:
            continue
        name = vocab.lang_alias.get(key)
        if name is None:
            rep.unknown_langs[raw] += 1
            name = raw
        if name not in langs:
            langs.append(name)
    if not langs:
        for prefix, defaults in sorted(vocab.folder_langs.items(), key=lambda x: -len(x[0])):
            if t.path.startswith(prefix + "/"):
                langs = list(defaults)
                break
    order = {n: i for i, n in enumerate(vocab.languages)}
    t.languages = sorted(langs, key=lambda n: order.get(n, 999))

    # Topics (+ subtopics)
    topics = []
    for raw in as_list(fm.get("topics")) + as_list(fm.get("subtopics")):
        key = norm_key(raw)
        if key in vocab.topic_drop:
            continue
        slug = vocab.topic_alias.get(key)
        if slug is None:
            rep.unknown_topics[raw.strip().strip("\"'")] += 1
            continue
        if slug not in topics:
            topics.append(slug)
    t.topics = topics

    # Data products -> system
    raw_dp = " ".join(as_list(first(fm, "dataProduct", "dataProducts", "dataProudct")))
    prods, seen = [], set()
    for m in DP_RE.finditer(raw_dp):
        base = f"DP{m.group(1)}.{m.group(2)}"
        if m.group(3):
            code = f"{base}.{m.group(3)}"
        else:
            # no revision given: assume .001, or the only revision that exists
            revs = sorted(c for c in vocab.products if c.startswith(base + "."))
            code = f"{base}.001" if f"{base}.001" in revs else (revs[0] if len(revs) == 1 else base)
            rep.no_revision[f"{base} -> {code}"] += 1
        if code in seen:
            continue
        seen.add(code)
        info = vocab.products.get(code)
        if info:
            prods.append({"code": code, "name": info["name"], "system": info["system"], "team": info.get("team", "")})
        else:
            rep.unknown_products[code] += 1
            digit = m.group(2)[0]
            sysguess = {"3": "AOP", "1": "OS", "0": "IS"}.get(digit, "General")
            prods.append({"code": code, "name": "", "system": sysguess, "team": ""})
    leftover = DP_RE.sub("", raw_dp).replace("NEON.", "")
    for junk in re.findall(r"DP\S+", leftover):
        rep.unknown_products[junk] += 1
    t.products = sorted(prods, key=lambda p: p["code"])
    systems = {p["system"] for p in prods if p["system"] in ("AOP", "OS", "IS")}
    t.derived_system = "Combined" if len(systems) > 1 else (systems.pop() if systems else "General")
    t.system = t.derived_system

    # Source file
    p = t.src
    stem = p.stem
    candidates = [
        ("rmd", p.with_suffix(".Rmd")),
        ("rmd", p.with_name(re.sub(r"-0$", "-content", stem) + ".Rmd")),
        ("ipynb", p.with_suffix(".ipynb")),
    ]
    for kind, c in candidates:
        if c.exists():
            t.source_kind, t.source_path = kind, c.relative_to(TUT).as_posix()
            break


def apply_meta(t: Tutorial, row: dict | None, vocab: Vocab) -> None:
    if not row:
        return
    t.theme = (row.get("theme") or "").strip()
    t.subtheme = (row.get("subtheme") or "").strip()
    t.task_types = [x.strip() for x in (row.get("task_types") or "").split(";") if x.strip() in vocab.task_types]
    sl = (row.get("skill_level") or "").strip()
    t.skill_level = sl if sl in vocab.skill_levels else ""
    ov = (row.get("system_override") or "").strip()
    if ov in vocab.systems:
        t.system = ov


# --------------------------------------------------------------------------- content fixes

LIST_RE = re.compile(r"^\s*([-*+]|\d+\.)\s")
FENCE_RE = re.compile(r"^\s*(```|~~~)")


def fence_indented_code(body: str) -> str:
    """Convert knitr 'strict' markdown (4-space indented code, often with every
    line separated by a blank line) into fenced ```r blocks. Lines starting with
    '## ' are knitr output and get their own plain fence."""
    lines = body.split("\n")
    out: list[str] = []
    i, n = 0, len(lines)
    in_fence = False
    in_html_depth = 0
    prev_text = ""  # last non-blank line emitted outside code

    def indented(s: str) -> bool:
        return s.startswith("    ") or s.startswith("\t")

    while i < n:
        line = lines[i]
        if FENCE_RE.match(line):
            in_fence = not in_fence
            out.append(line)
            i += 1
            continue
        if in_fence:
            out.append(line)
            i += 1
            continue
        stripped = line.strip()
        # track raw HTML blocks (pre/table/script/style) we must not touch
        if re.match(r"<(pre|script|style|table)\b", stripped, re.I):
            in_html_depth += 1
        if in_html_depth:
            out.append(line)
            if re.search(r"</(pre|script|style|table)>", stripped, re.I):
                in_html_depth -= 1
            i += 1
            continue

        starts_block = (
            indented(line)
            and stripped
            and (not out or out[-1].strip() == "")
            and not LIST_RE.match(prev_text)
            and not (prev_text.startswith("    ") or prev_text.startswith("\t"))
        )
        if not starts_block:
            out.append(line)
            if stripped:
                prev_text = line
            i += 1
            continue

        # collect block: indented lines; "" lines are separators if followed by more indented lines
        # knitr strict output separates every code line with an empty line; a line of
        # only spaces (or 2+ empty lines) marks a real blank line in the code.
        block: list[str] = []
        while i < n:
            ln = lines[i]
            if ln.strip():
                if not indented(ln):
                    break
                block.append(ln)
                i += 1
                continue
            j = i
            while j < n and not lines[j].strip():
                j += 1
            if j < n and indented(lines[j]):
                gap = lines[i:j]
                if (any(g for g in gap) or len(gap) >= 2) and block and block[-1] != "<<GAP>>":
                    block.append("<<GAP>>")
                i = j
            else:
                break
        # dedent + split into code/output runs
        runs: list[tuple[str, list[str]]] = []
        for ln in block:
            if ln == "<<GAP>>":
                text, kind = "", None
            else:
                text = ln[4:] if ln.startswith("    ") else ln.lstrip("\t")
                kind = "out" if text.startswith("## ") or text == "##" else "code"
            if kind is None:
                if runs:
                    runs[-1][1].append("")
                continue
            if not text.strip():
                if runs:
                    runs[-1][1].append("")
                continue
            if not runs or runs[-1][0] != kind:
                runs.append((kind, []))
            runs[-1][1].append(text)
        for kind, rl in runs:
            while rl and not rl[-1].strip():
                rl.pop()
            if not rl:
                continue
            out.append("```r" if kind == "code" else "```text")
            out.extend(rl)
            out.append("```")
            out.append("")
        prev_text = ""
    return "\n".join(out)


def fix_links(body: str) -> str:
    body = re.sub(r"\{\{\s*base\.url\s*\}\}", "https://www.neonscience.org", body)
    body = re.sub(r'(href|src)="/(?!/)', r'\1="https://www.neonscience.org/', body)
    body = re.sub(r"\]\(/(?!/)", "](https://www.neonscience.org/", body)
    # protect {{ ... }} text from any future macro plugins / jinja
    return body


# --------------------------------------------------------------------------- page output


def rel_prefix(t: Tutorial) -> str:
    depth = t.page_url.count("/")
    return "../" * depth


def chip(prefix: str, facet: str, value: str, label: str, cls: str) -> str:
    href = f"{prefix}browse/?{facet}={html.escape(value, quote=True)}"
    return f'<a class="nds-chip nds-chip--{cls}" href="{href}">{html.escape(label)}</a>'


def card_html(t: Tutorial, vocab: Vocab) -> str:
    fm = t.fm
    pre = rel_prefix(t)
    chips = []
    for lg in t.languages:
        chips.append(chip(pre, "language", lg, lg, "lang"))
    chips.append(chip(pre, "system", t.system, t.system, "system"))
    if t.skill_level:
        chips.append(chip(pre, "skill", t.skill_level, vocab.skill_levels[t.skill_level], "skill"))
    for tt in t.task_types:
        chips.append(chip(pre, "task", tt, vocab.task_types[tt], "task"))
    for tp in t.topics:
        chips.append(chip(pre, "topic", tp, vocab.topics[tp], "topic"))

    rows = []

    def row(label, value):
        if value:
            rows.append(f"<tr><th>{label}</th><td>{value}</td></tr>")

    esc = lambda v: html.escape(", ".join(as_list(v)))  # noqa: E731
    row("Authors", esc(fm.get("authors")))
    row("Contributors", esc(fm.get("contributors")))
    row("Estimated time", esc(fm.get("estimatedTime")))
    row("Packages", esc(first(fm, "packagesLibraries", "packagesLibaries")))
    if t.products:
        row("Data products", ", ".join(
            f'<a href="https://data.neonscience.org/data-products/{p["code"]}" title="{html.escape(p["name"])}">{p["code"]}</a>'
            for p in t.products))
    if t.series:
        row("Series", html.escape(t.series))
    links = []
    if t.url_title:
        links.append(f'<a href="{NEON_TUTORIAL_URL}{t.url_title}">neonscience.org</a>')
    if t.source_path:
        label = {"rmd": "R Markdown source", "ipynb": "Jupyter notebook"}[t.source_kind]
        links.append(f'<a href="{GITHUB_BLOB}{t.source_path}">{label}</a>')
    for k in sorted(k for k in fm if re.fullmatch(r"code\d+", k)):
        for u in as_list(fm[k]):
            if u.startswith("http"):
                links.append(f'<a href="{html.escape(u)}">{html.escape(PurePosixPath(u).name)}</a>')
    row("Links", " · ".join(links))
    return (
        '<div class="nds-card">\n'
        f'<div class="nds-chips">{"".join(chips)}</div>\n'
        f'<table class="nds-meta">{"".join(rows)}</table>\n'
        "</div>\n"
    )


HTML_PRESERVE_RE = re.compile(r"<!--html_preserve-->(.*?)<!--/html_preserve-->", re.S)
HEADING_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6"]
PRE_LEXERS = {"r": "r", "python": "python", "py": "python", "bash": "bash", "sh": "bash"}


def convert_rmd_html(fragment: str, page_stem: str, dest_dir: Path, counter: list[int]) -> str:
    """Make an rmarkdown HTML fragment (the 'Tabbed' tutorials' includeHTML output)
    render natively in MkDocs Material:
      - rmarkdown `.tabset` sections -> Material tabbed blocks (pymdownx.tabbed markup)
      - <pre class="r|python"><code> -> Pygments-highlighted Material code blocks
      - base64 data: images -> image files next to the page
    """
    from bs4 import BeautifulSoup
    from pygments import highlight
    from pygments.formatters import HtmlFormatter
    from pygments.lexers import get_lexer_by_name

    soup = BeautifulSoup(fragment, "html.parser")

    # 1. inline base64 images -> files
    for img in soup.find_all("img", src=re.compile(r"^data:image/")):
        m = re.match(r"data:image/([a-z+]+);base64,(.*)", img["src"], re.S)
        if not m:
            continue
        ext = {"svg+xml": "svg", "jpeg": "jpg"}.get(m.group(1), m.group(1))
        counter[1] += 1
        name = f"{page_stem}_img/img-{counter[1]}.{ext}"
        target = dest_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(m.group(2)))
        img["src"] = "../" + name  # page is served at <stem>/ (use_directory_urls)

    # 2. highlight code blocks
    fmt = HtmlFormatter(nowrap=True)
    for pre in soup.find_all("pre", class_=True):
        lang = next((PRE_LEXERS[c] for c in pre.get("class", []) if c in PRE_LEXERS), None)
        if not lang:
            continue
        code = pre.get_text()
        hl = highlight(code, get_lexer_by_name(lang), fmt)
        new = BeautifulSoup(
            f'<div class="language-{lang} highlight"><pre><span></span><code>{hl}</code></pre></div>', "html.parser")
        pre.replace_with(new)

    # 3. empty headings from rmarkdown `## {-}` tabset terminators
    for h in soup.find_all(HEADING_TAGS):
        if not h.get_text(strip=True):
            h.decompose()

    # 4. tabsets (reverse document order = nested tabsets first)
    for ts in reversed(soup.find_all("div", class_="tabset")):
        sections = [c for c in ts.find_all("div", class_="section", recursive=False)]
        if not sections:
            continue
        counter[0] += 1
        n = counter[0]
        labels, blocks = [], []
        for k, sec in enumerate(sections, 1):
            h = sec.find(HEADING_TAGS)
            label = h.get_text(strip=True) if h else f"Tab {k}"
            if h:
                h.decompose()
            labels.append(label)
            blocks.append(sec.decode_contents())
            sec.decompose()
        inputs = "".join(
            '<input{} id="__tabbed_{n}_{k}" name="__tabbed_{n}" type="radio"/>'.format(
                ' checked="checked"' if k == 1 else "", n=n, k=k)
            for k in range(1, len(labels) + 1))
        label_html = "".join(f'<label for="__tabbed_{n}_{k}">{html.escape(lb)}</label>' for k, lb in enumerate(labels, 1))
        block_html = "".join(f'<div class="tabbed-block">{b}</div>' for b in blocks)
        tabbed = BeautifulSoup(
            f'<div class="tabbed-set tabbed-alternate" data-tabs="{n}:{len(labels)}">{inputs}'
            f'<div class="tabbed-labels">{label_html}</div><div class="tabbed-content">{block_html}</div></div>',
            "html.parser")
        ts.append(tabbed)
        ts["class"] = [c for c in ts["class"] if c != "tabset"]
        for h in ts.find_all(HEADING_TAGS, class_="tabset", recursive=False):
            h["class"] = [c for c in h["class"] if c != "tabset"] or None
            if not h["class"]:
                del h["class"]
    return str(soup)


def write_page(t: Tutorial, vocab: Vocab) -> None:
    body = t.body
    if "html_preserve" not in body and (t.source_kind == "rmd" or (t.path.startswith("R/") and "```" not in body)):
        body = fence_indented_code(body)
    body = fix_links(body)
    if "<!--html_preserve-->" in body:
        dest_dir = (OUT / t.path).parent
        counter = [1000, 0]
        stem = PurePosixPath(t.path).stem
        body = HTML_PRESERVE_RE.sub(lambda m: convert_rmd_html(m.group(1), stem, dest_dir, counter), body)
    new_fm = {
        "title": t.title,
        "description": t.description,
        "tags": [vocab.topics[x] for x in t.topics],
    }
    if not new_fm["description"]:
        del new_fm["description"]
    head = "---\n" + yaml.safe_dump(new_fm, sort_keys=False, allow_unicode=True) + "---\n\n"
    dest = OUT / t.path
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(head + card_html(t, vocab) + "\n" + body.lstrip("\n"), encoding="utf-8")
    copy_local_assets(t, body, dest.parent)


LOCAL_REF_RE = re.compile(r"""(?:\]\(|src=["'])(?!https?:|data:|#|/|mailto:)([^)"'\s]+?\.(?:png|jpe?g|gif|svg|webp))""", re.I)


def copy_local_assets(t: Tutorial, body: str, dest_dir: Path) -> None:
    """Copy images referenced by relative path (e.g. nbconvert '<name>_files/x.png')."""
    for ref in set(LOCAL_REF_RE.findall(body)):
        src = (t.src.parent / ref).resolve()
        if src.is_file() and TUT in src.parents:
            target = dest_dir / ref
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)


# --------------------------------------------------------------------------- browse pages


def link_from_browse(t: Tutorial) -> str:
    return f"[{t.title}](../{t.path})"


def item_line(t: Tutorial, vocab: Vocab) -> str:
    bits = [", ".join(t.languages)] if t.languages else []
    if t.skill_level:
        bits.append(vocab.skill_levels[t.skill_level])
    suffix = f" — *{' · '.join(bits)}*" if bits else ""
    return f"- {link_from_browse(t)}{suffix}"


def write_grouped(fname: str, title: str, intro: str, groups: list[tuple[str, list[Tutorial]]], vocab: Vocab):
    lines = ["---", f"title: {title}", "---", "", f"# {title}", "", intro, ""]
    for heading, items in groups:
        if not items:
            continue
        lines += [f"## {heading} ({len(items)})", ""]
        lines += [item_line(t, vocab) for t in sorted(items, key=lambda x: x.title.lower())]
        lines.append("")
    (OUT / "browse" / fname).write_text("\n".join(lines), encoding="utf-8")


def write_browse(tuts: list[Tutorial], vocab: Vocab) -> None:
    (OUT / "browse").mkdir(parents=True, exist_ok=True)

    # Languages
    by_lang = defaultdict(list)
    for t in tuts:
        for lg in t.languages or ["Unassigned"]:
            by_lang[lg].append(t)
    order = vocab.languages + sorted(set(by_lang) - set(vocab.languages))
    write_grouped("languages.md", "Browse by Language / Tool",
                  "Tutorials grouped by programming language or software tool.",
                  [(lg, by_lang[lg]) for lg in order if lg in by_lang], vocab)

    # Topics
    by_topic = defaultdict(list)
    for t in tuts:
        for tp in t.topics:
            by_topic[tp].append(t)
    write_grouped("topics.md", "Browse by Topic",
                  "Tutorials grouped by topic. See also the [tag index](../tags.md).",
                  [(vocab.topics[s], by_topic[s]) for s in vocab.topics] +
                  [("Unassigned", [t for t in tuts if not t.topics])], vocab)

    # Task types
    by_task = defaultdict(list)
    for t in tuts:
        for tt in t.task_types or ["_none"]:
            by_task[tt].append(t)
    write_grouped("tasks.md", "Browse by Workflow / Task",
                  "Tutorials grouped by the kind of task they teach.",
                  [(vocab.task_types[s], by_task[s]) for s in vocab.task_types] +
                  [("Unassigned", by_task["_none"])], vocab)

    # Skill level
    by_skill = defaultdict(list)
    for t in tuts:
        by_skill[t.skill_level or "_none"].append(t)
    write_grouped("skill-levels.md", "Browse by Skill Level",
                  "Tutorials grouped by expected experience level.",
                  [(vocab.skill_levels[s], by_skill[s]) for s in vocab.skill_levels] +
                  [("Unassigned", by_skill["_none"])], vocab)

    # Themes (theme -> subtheme)
    themes = defaultdict(lambda: defaultdict(list))
    for t in tuts:
        themes[t.theme or "Unassigned"][t.subtheme].append(t)
    lines = ["---", "title: Browse by Theme", "---", "", "# Browse by Theme", "",
             "Tutorials organized into thematic collections.", ""]
    for theme in sorted(themes, key=lambda x: (x == "Unassigned", x)):
        total = sum(len(v) for v in themes[theme].values())
        lines += [f"## {theme} ({total})", ""]
        for sub in sorted(themes[theme]):
            if sub:
                lines += [f"### {sub}", ""]
            lines += [item_line(t, vocab) for t in sorted(themes[theme][sub], key=lambda x: x.title.lower())]
            lines.append("")
    (OUT / "browse" / "themes.md").write_text("\n".join(lines), encoding="utf-8")

    # Systems -> data products
    lines = ["---", "title: Browse by System & Data Product", "---", "",
             "# Browse by System & Data Product", "",
             "NEON collects data with three systems: the **Airborne Observation Platform (AOP)**, "
             "the **Observational Sampling (OS)** system and the **Instrumented Sampling (IS)** system. "
             "Tutorials using data from more than one system are listed under **Combined**, and also "
             "appear under each data product they use.", ""]
    for sysslug, sysname in vocab.systems.items():
        members = [t for t in tuts if t.system == sysslug]
        if sysslug in ("AOP", "OS", "IS"):
            # include tutorials (incl. Combined) that use a product from this system
            prod_map = defaultdict(list)
            for t in tuts:
                for p in t.products:
                    if p["system"] == sysslug:
                        prod_map[p["code"]].append((t, p))
            no_prod = [t for t in members if not any(p["system"] == sysslug for p in t.products)]
            count = len({id(t) for v in prod_map.values() for t, _ in v} | {id(t) for t in no_prod})
            if not count:
                continue
            lines += [f"## {SYSTEM_ICONS[sysslug]} {sysname} ({count}) {{ #{sysslug.lower()} }}", ""]
            for code in sorted(prod_map):
                name = prod_map[code][0][1]["name"]
                lines += [f"### {code}{' — ' + name if name else ''}", ""]
                lines += [item_line(t, vocab) for t, _ in sorted(prod_map[code], key=lambda x: x[0].title.lower())]
                lines.append("")
            if no_prod:
                lines += ["### Concepts & general (no specific data product)", ""]
                lines += [item_line(t, vocab) for t in sorted(no_prod, key=lambda x: x.title.lower())]
                lines.append("")
        else:
            if not members:
                continue
            lines += [f"## {sysname} ({len(members)}) {{ #{sysslug.lower()} }}", ""]
            lines += [item_line(t, vocab) for t in sorted(members, key=lambda x: x.title.lower())]
            lines.append("")
    (OUT / "browse" / "systems.md").write_text("\n".join(lines), encoding="utf-8")

    # Catalog for the filter page
    catalog = {
        "facets": {
            "language": [{"value": x, "label": x} for x in order if x in by_lang and x != "Unassigned"],
            "topic": [{"value": s, "label": n} for s, n in vocab.topics.items() if s in by_topic],
            "system": [{"value": s, "label": n} for s, n in vocab.systems.items()],
            "task": [{"value": s, "label": n} for s, n in vocab.task_types.items()],
            "skill": [{"value": s, "label": n} for s, n in vocab.skill_levels.items()],
            "product": sorted(
                ({"value": p["code"], "label": f'{p["code"]} {p["name"]}'.strip(), "system": p["system"]}
                 for p in {p["code"]: p for t in tuts for p in t.products}.values()),
                key=lambda x: (x["system"], x["value"])),
        },
        "tutorials": [
            {
                "title": t.title,
                "description": t.description,
                "url": t.page_url,
                "language": t.languages,
                "topic": t.topics,
                "system": t.system,
                "product": [p["code"] for p in t.products],
                "productSystems": sorted({p["system"] for p in t.products}),
                "task": t.task_types,
                "skill": t.skill_level,
                "theme": t.theme,
            }
            for t in sorted(tuts, key=lambda x: x.title.lower())
        ],
    }
    (OUT / "browse" / "catalog.json").write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------- nav


def humanize(name: str) -> str:
    if name in NAV_LABELS:
        return NAV_LABELS[name]
    s = re.sub(r"^\d+[-_.]?\s*(?=[A-Za-z])", "", name)  # leading "1-", "01_"
    s = s.replace("_", " ").replace("-", " ").strip()
    return s[:1].upper() + s[1:] if s else name


def build_tree(tuts: list[Tutorial]) -> dict:
    root: dict = {}
    for t in tuts:
        parts = t.path.split("/")
        node = root
        for d in parts[:-1]:
            node = node.setdefault(d, {})
        node[parts[-1]] = t
    return root


def tree_to_nav(node: dict) -> list:
    items = []
    for key in sorted(node, key=lambda k: (isinstance(node[k], dict), k.lower())):
        val = node[key]
        if isinstance(val, Tutorial):
            items.append({val.title: val.path})
        else:
            children = tree_to_nav(val)
            if len(children) == 1:  # collapse single-child folders
                items.append(children[0])
            else:
                items.append({humanize(key): children})
    return items


def write_nav(tuts: list[Tutorial]) -> None:
    tree = build_tree(tuts)
    nav = [
        {"Home": "index.md"},
        {"Find a Tutorial": "browse/index.md"},
        {"Browse": [
            {"By Theme": "browse/themes.md"},
            {"By Topic": "browse/topics.md"},
            {"By Language / Tool": "browse/languages.md"},
            {"By System & Data Product": "browse/systems.md"},
            {"By Workflow / Task": "browse/tasks.md"},
            {"By Skill Level": "browse/skill-levels.md"},
            {"Tag Index": "tags.md"},
        ]},
    ]
    for top in ["Python", "R", "GEE", "Tabbed", "Other"]:
        if top in tree:
            nav.append({humanize(top): tree_to_nav(tree.pop(top))})
    for top in sorted(tree):  # anything else
        val = tree[top]
        nav.append({val.title: val.path} if isinstance(val, Tutorial) else {humanize(top): tree_to_nav(val)})
    nav.append({"About": "about.md"})
    (OUT / ".nav.yml").write_text(yaml.safe_dump(nav, sort_keys=False, allow_unicode=True), encoding="utf-8")


# --------------------------------------------------------------------------- tutorial_meta.csv


def read_meta() -> list[dict]:
    if not META_CSV.exists():
        return []
    with open(META_CSV, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_meta(rows: list[dict]) -> None:
    with open(META_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=META_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in sorted(rows, key=lambda r: r["path"]):
            w.writerow({k: r.get(k, "") for k in META_FIELDS})


def match_meta(tuts: list[Tutorial], rows: list[dict]) -> tuple[dict, list[dict]]:
    """Return {tutorial path: row} and the rows that matched nothing (orphans).
    Rows match by path first, then by unique sync_id (handles moved files)."""
    by_path = {r["path"]: r for r in rows}
    sid_count = Counter(t.sync_id for t in tuts if t.sync_id)
    by_sid = {r["sync_id"]: r for r in rows if r.get("sync_id")}
    matched, used = {}, set()
    for t in tuts:
        r = by_path.get(t.path)
        if r is None and t.sync_id and sid_count[t.sync_id] == 1:
            r = by_sid.get(t.sync_id)
            if r is not None and r["path"] in {x.path for x in tuts}:
                r = None  # that row belongs to an existing file
        if r is not None:
            matched[t.path] = r
            used.add(id(r))
    return matched, [r for r in rows if id(r) not in used]


TOC_ROW = re.compile(r"^\|\s*(?P<title>[^|]+?)\s*\|\s*\[[^\]]*\]\((?P<path>[^)]+)\)\s*\|\s*(?:\[[^\]]*\]\((?P<web>[^)]*)\))?")


def parse_toc(path: Path, level_map=None) -> dict:
    """Parse TOC-by-*.md tables -> {key: (h2, h3)} keyed by md path and by urlTitle."""
    out = {}
    if not path.exists():
        return out
    h2 = h3 = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            h2, h3 = re.sub(r"^[A-Z]\.\s*", "", line[3:].strip()), ""
        elif line.startswith("### "):
            h3 = re.sub(r"^[A-Z]\d+\.\s*", "", line[4:].strip())
        else:
            m = TOC_ROW.match(line)
            if m:
                p = m.group("path")
                p = re.sub(r"\.Rmd$", ".md", p)
                p = re.sub(r"-content\.md$", "-0.md", p)
                out[p] = (h2, h3)
                if m.group("web") and "/tutorials/" in m.group("web"):
                    out["url:" + m.group("web").rsplit("/", 1)[-1]] = (h2, h3)
    return out


def guess_task_types(t: Tutorial, vocab: Vocab) -> list[str]:
    text = f"{t.title} {t.description}".lower()
    scores = Counter()
    for slug, kws in vocab.task_keywords.items():
        for kw in kws:
            if kw in text:
                scores[slug] += 1
    picked = [s for s, _ in scores.most_common(2)]
    return picked or ["explore-visualize"]


def guess_skill(t: Tutorial) -> str:
    text = f"{t.title} {t.description} {t.series}".lower()
    if re.search(r"\b(intro|introduction|basics|what is|getting started|set ?up|install|overview|primer|concept)", text):
        return "intro"
    if re.search(r"\b(random forest|machine learning|classif|uncertainty|model|forecast|brdf|batch|cloud|bigquery|docker)", text):
        return "advanced"
    if re.match(r"(0?1|1a)[-_ ]", PurePosixPath(t.path).name):
        return "intro"
    return "intermediate"


def init_meta(tuts: list[Tutorial], vocab: Vocab) -> None:
    rows = read_meta()
    matched, orphans = match_meta(tuts, rows)
    themes = parse_toc(TUT / "TOC-by-theme.md")
    systems = parse_toc(TUT / "TOC-by-system.md")
    sys_map = {"AOP": "AOP", "OS": "OS", "IS": "IS", "Cross-System": "Combined", "General": "General"}
    added = 0
    for t in tuts:
        if t.path in matched:
            r = matched[t.path]
            r["path"], r["sync_id"], r["title"], r["url_title"] = t.path, t.sync_id, t.title, t.url_title
            continue
        th = themes.get(t.path) or themes.get("url:" + t.url_title) or ("", "")
        sy = systems.get(t.path) or systems.get("url:" + t.url_title)
        override = ""
        if sy:
            key = sy[0].split(" ")[0].split("—")[0].strip()
            sysv = sys_map.get(key, "")
            # only fill in a system for tutorials with no data products (concept pages, etc.)
            if sysv and t.derived_system == "General" and sysv != "General":
                override = sysv
        rows.append({
            "path": t.path, "sync_id": t.sync_id, "title": t.title, "url_title": t.url_title,
            "theme": th[0], "subtheme": th[1],
            "task_types": ";".join(guess_task_types(t, vocab)),
            "skill_level": guess_skill(t),
            "system_override": override, "reviewed": "", "notes": "",
        })
        added += 1
    for r in orphans:
        if "ORPHAN" not in (r.get("notes") or ""):
            r["notes"] = ("ORPHAN: file not found. " + (r.get("notes") or "")).strip()
    write_meta(rows)
    print(f"tutorial_meta.csv: {added} rows added, {len(orphans)} orphaned, {len(rows)} total")


# --------------------------------------------------------------------------- products


def refresh_products(tuts: list[Tutorial]) -> None:
    print("Fetching", NEON_API_PRODUCTS)
    with urllib.request.urlopen(NEON_API_PRODUCTS, timeout=60) as r:
        data = json.load(r)["data"]
    team_to_sys = {"AOP": "AOP", "TOS": "OS", "AOS": "OS", "TIS": "IS", "AIS": "IS"}
    used = set()
    for t in tuts:
        raw = " ".join(as_list(first(t.fm, "dataProduct", "dataProducts", "dataProudct")))
        for m in DP_RE.finditer(raw):
            used.add(f"DP{m.group(1)}.{m.group(2)}")
    prods = []
    for d in data:
        team = d.get("productScienceTeamAbbr", "")
        prods.append({
            "code": d["productCode"],
            "name": d["productName"],
            "team": team,
            "system": team_to_sys.get(team, "General"),
            "used": d["productCode"][:9] in used,
        })
    prods.sort(key=lambda p: p["code"])
    header = ("# Generated by `python mkdocs/build_docs.py --refresh-products` from the NEON API.\n"
              "# Maps data product codes to names and NEON systems (AOP / OS / IS).\n"
              "# Add retired or missing products by hand (they will be kept only if you re-add them after a refresh).\n")
    (VOCAB / "data_products.yml").write_text(
        header + yaml.safe_dump({"products": prods}, sort_keys=False, allow_unicode=True, width=200),
        encoding="utf-8")
    print(f"wrote {len(prods)} products ({sum(p['used'] for p in prods)} used by tutorials)")


# --------------------------------------------------------------------------- staleness


def git_time(path: Path) -> int:
    try:
        out = subprocess.run(["git", "log", "-1", "--format=%ct", "--", str(path)],
                             cwd=REPO, capture_output=True, text=True, timeout=20).stdout.strip()
        return int(out) if out else 0
    except Exception:
        return 0


def check_stale(tuts: list[Tutorial], rep: Report) -> None:
    for t in tuts:
        if not t.source_path:
            continue
        src_t, md_t = git_time(TUT / t.source_path), git_time(t.src)
        if src_t and md_t and src_t > md_t + 60:
            rep.stale.append(f"{t.source_path} (source committed after .md)")


# --------------------------------------------------------------------------- main


def build(tuts: list[Tutorial], vocab: Vocab, rep: Report, with_stale: bool) -> None:
    # clear contents (not the folder itself, which may be held open by `mkdocs serve` on Windows)
    OUT.mkdir(exist_ok=True)
    for child in OUT.iterdir():
        shutil.rmtree(child) if child.is_dir() else child.unlink()
    shutil.copytree(PAGES, OUT, dirs_exist_ok=True)
    rows = read_meta()
    matched, orphans = match_meta(tuts, rows)
    for t in tuts:
        apply_meta(t, matched.get(t.path), vocab)
        write_page(t, vocab)
    write_browse(tuts, vocab)
    write_nav(tuts)
    if with_stale:
        check_stale(tuts, rep)

    # ----- report
    print(f"\nBuilt {len(tuts)} tutorial pages into {OUT.relative_to(REPO)}")
    print(f"  systems: {dict(Counter(t.system for t in tuts))}")
    print(f"  languages: {dict(Counter(lg for t in tuts for lg in t.languages))}")
    if not META_CSV.exists():
        print("  NOTE: mkdocs/tutorial_meta.csv not found - run with --init-meta to create it")

    def section(title, items):
        if items:
            print(f"\n{title} ({len(items)}):")
            for x in items:
                print("  -", x)

    section("Front matter missing or unparseable", rep.bad_fm)
    section("Unknown topics (add to vocab/topics.yml aliases or drop)",
            [f"{k!r} x{v}" for k, v in rep.unknown_topics.most_common()])
    section("Unknown languages (add to vocab/languages.yml)",
            [f"{k!r} x{v}" for k, v in rep.unknown_langs.most_common()])
    section("Unrecognized data product IDs", [f"{k!r} x{v}" for k, v in rep.unknown_products.most_common()])
    section("Data product IDs without a revision (assumed; add the revision in front matter)",
            [f"{k} x{v}" for k, v in sorted(rep.no_revision.items())])
    section("Not in tutorial_meta.csv", [t.path for t in tuts if t.path not in matched] if rows else [])
    section("Orphaned tutorial_meta.csv rows", [r["path"] for r in orphans])
    missing = Counter()
    for t in tuts:
        missing["theme"] += not t.theme
        missing["task_types"] += not t.task_types
        missing["skill_level"] += not t.skill_level
    print(f"\nMissing curated values: {dict(missing)}")
    if rows:
        print(f"Reviewed rows in tutorial_meta.csv: {sum(1 for r in rows if (r.get('reviewed') or '').strip())}/{len(rows)}")
    section("Possibly stale .md (source newer)", rep.stale)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--init-meta", action="store_true", help="add new tutorials to tutorial_meta.csv (never overwrites curated values)")
    ap.add_argument("--refresh-products", action="store_true", help="refresh vocab/data_products.yml from the NEON API")
    ap.add_argument("--stale", action="store_true", help="report .md files older than their .Rmd/.ipynb (uses git log; slower)")
    args = ap.parse_args(argv)

    vocab = load_vocab()
    tuts = discover(vocab)
    if args.refresh_products:
        refresh_products(tuts)
        vocab = load_vocab()
    rep = Report()
    for t in tuts:
        normalize(t, vocab, rep)
    if args.init_meta:
        init_meta(tuts, vocab)
    build(tuts, vocab, rep, args.stale)
    return 0


if __name__ == "__main__":
    sys.exit(main())
