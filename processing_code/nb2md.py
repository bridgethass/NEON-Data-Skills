"""Convert NEON-Data-Skills Jupyter notebooks to .md (+ .py, .html) for publishing.

Python replacement for 02process-ipynb-2-MD_NDSRepo.R. Output format is unchanged
(front matter from the first markdown cell, figures in <name>_files/, figure links
pointing at raw.githubusercontent.com), so the neonscience.org import keeps working.
Works on Windows/macOS/Linux; no hard-coded paths.

Run it from an environment that has jupyter + the packages/kernel the notebooks need.

Examples (from anywhere inside the repo):
    python processing_code/nb2md.py tutorials/Python/AOP/Lidar/intro-lidar      # all notebooks under a folder
    python processing_code/nb2md.py path/to/notebook.ipynb --no-execute         # convert using saved outputs
    python processing_code/nb2md.py --changed                                   # notebooks changed vs origin/main
    python processing_code/nb2md.py --stale --dry-run                           # list notebooks newer than their .md

Per notebook:
  1. check the first cell holds YAML front matter (--- ... ---)
  2. clear outputs and execute in the notebook's folder (unless --no-execute)
  3. export .md (+ .py, .html); figures -> <name>_files/ (old <name>_* figures there are removed)
  4. rewrite figure links in the .md to https://raw.githubusercontent.com/NEONScience/NEON-Data-Skills/<branch>/tutorials/...
  5. clear outputs in the saved .ipynb (unless --keep-outputs)
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

import nbformat
import yaml
from nbconvert import HTMLExporter, MarkdownExporter, ScriptExporter
from nbconvert.preprocessors import CellExecutionError, ClearOutputPreprocessor, ExecutePreprocessor

RAW_BASE = "https://raw.githubusercontent.com/NEONScience/NEON-Data-Skills/{branch}/"
LARGE_OUTPUT_BYTES = 1_000_000


def repo_root(start: Path) -> Path:
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=start, capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit("nb2md: not inside a git repository")
    return Path(out.stdout.strip()).resolve()


def find_notebooks(paths: list[str]) -> list[Path]:
    found: list[Path] = []
    for p in paths:
        path = Path(p).resolve()
        if path.is_dir():
            found += sorted(path.rglob("*.ipynb"))
        elif path.suffix == ".ipynb" and path.exists():
            found.append(path)
        else:
            print(f"  ! skipping {p} (not a notebook or folder)")
    return [p for p in found if ".ipynb_checkpoints" not in p.parts]


def git_lines(root: Path, *args: str) -> list[str]:
    out = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    return [x for x in out.stdout.splitlines() if x.strip()]


def changed_notebooks(root: Path, base: str) -> list[Path]:
    names = set(git_lines(root, "diff", "--name-only", f"{base}...HEAD"))
    names |= set(git_lines(root, "diff", "--name-only"))  # unstaged
    names |= set(git_lines(root, "diff", "--name-only", "--cached"))  # staged
    names |= set(git_lines(root, "ls-files", "--others", "--exclude-standard"))  # untracked
    nbs = [root / n for n in names if n.endswith(".ipynb") and n.startswith("tutorials/")]
    return sorted(p for p in nbs if p.exists() and ".ipynb_checkpoints" not in p.parts)


def last_commit_time(root: Path, path: Path) -> int:
    out = git_lines(root, "log", "-1", "--format=%ct", "--", str(path))
    return int(out[0]) if out else 0


def stale_notebooks(root: Path) -> list[Path]:
    """Notebooks whose last change (commit or uncommitted edit) is newer than their .md."""
    dirty = {root / n for n in git_lines(root, "diff", "--name-only") + git_lines(root, "diff", "--name-only", "--cached")}
    res = []
    for nb in sorted((root / "tutorials").rglob("*.ipynb")):
        if ".ipynb_checkpoints" in nb.parts:
            continue
        md = nb.with_suffix(".md")
        if not md.exists():
            res.append(nb)
        elif nb in dirty and md not in dirty:
            res.append(nb)
        elif last_commit_time(root, nb) > last_commit_time(root, md) + 60:
            res.append(nb)
    return res


def check_front_matter(nb) -> str | None:
    """Return a problem description, or None if the first cell holds valid front matter."""
    if not nb.cells:
        return "notebook is empty"
    cell = nb.cells[0]
    src = cell.source.lstrip("﻿").strip()
    if cell.cell_type != "markdown" or not src.startswith("---"):
        return "first cell is not a markdown cell starting with '---' (YAML front matter)"
    m = re.match(r"---[ \t]*\n(.*?)\n---[ \t]*(\n|$)", src.replace("\r\n", "\n"), re.S)
    if not m:
        return "front matter in first cell is not closed with '---'"
    try:
        fm = yaml.safe_load(m.group(1))
    except yaml.YAMLError as e:
        return f"front matter is not valid YAML ({str(e).splitlines()[0]}) - tabs are a common cause"
    if not isinstance(fm, dict) or not fm.get("title"):
        return "front matter has no title"
    return None


def large_outputs(nb) -> list[str]:
    warn = []
    for i, cell in enumerate(nb.cells):
        size = sum(len(str(o)) for o in cell.get("outputs", []))
        if size > LARGE_OUTPUT_BYTES:
            warn.append(f"cell {i}: outputs are {size / 1e6:.1f} MB (interactive/HTML output?)")
    return warn


def rewrite_figure_links(md: str, files_dir: str, url_prefix: str) -> str:
    """Point relative '<name>_files/...' references at the raw GitHub URL."""
    pat = re.compile(r"(\]\(|src=[\"'])(?:\./)?" + re.escape(files_dir) + r"/([^)\"'\s]+)")
    return pat.sub(lambda m: f"{m.group(1)}{url_prefix}{files_dir}/{m.group(2)}", md)


def convert(nb_path: Path, root: Path, args) -> tuple[bool, str]:
    t0 = time.time()
    nb_dir, stem = nb_path.parent, nb_path.stem
    rel_dir = nb_dir.relative_to(root).as_posix()
    files_dir = f"{stem}_files"
    url_prefix = RAW_BASE.format(branch=args.branch) + quote(rel_dir) + "/"
    notes = []

    nb = nbformat.read(nb_path, as_version=4)
    problem = check_front_matter(nb)
    if problem:
        if args.strict:
            return False, f"front matter: {problem}"
        notes.append(f"WARNING front matter: {problem}")

    if args.dry_run:
        return True, "would convert" + ("" if not notes else " | " + "; ".join(notes))

    if args.execute:
        ClearOutputPreprocessor().preprocess(nb, {})
        ep = ExecutePreprocessor(timeout=args.timeout, kernel_name=args.kernel)
        try:
            ep.preprocess(nb, {"metadata": {"path": str(nb_dir)}})
        except CellExecutionError as e:
            msg = str(e).strip().splitlines()
            tail = next((ln for ln in reversed(msg) if ln.strip()), "")
            return False, f"execution failed: {tail[:200]}"

    notes += [f"WARNING {w}" for w in large_outputs(nb)]
    formats = set(args.formats.split(","))

    if "md" in formats:
        resources = {"unique_key": stem, "output_files_dir": files_dir}
        body, res = MarkdownExporter().from_notebook_node(nb, resources=resources)
        out_dir = nb_dir / files_dir
        # remove figures from previous runs so deleted plots don't linger
        if out_dir.is_dir():
            for old in out_dir.glob(f"{stem}_*"):
                old.unlink()
        for name, data in (res.get("outputs") or {}).items():
            target = nb_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        if out_dir.is_dir() and not any(out_dir.iterdir()):
            out_dir.rmdir()
        body = rewrite_figure_links(body, files_dir, url_prefix).lstrip()
        nb_path.with_suffix(".md").write_text(body, encoding="utf-8", newline="\n")
        notes.append(f"{len(res.get('outputs') or {})} figures")
    if "py" in formats:
        body, res = ScriptExporter().from_notebook_node(nb)
        nb_path.with_suffix(res.get("output_extension", ".py")).write_text(body, encoding="utf-8", newline="\n")
    if "html" in formats:
        body, _ = HTMLExporter().from_notebook_node(nb)
        nb_path.with_suffix(".html").write_text(body, encoding="utf-8", newline="\n")

    if not args.keep_outputs:
        ClearOutputPreprocessor().preprocess(nb, {})
        for cell in nb.cells:
            if cell.cell_type == "code":
                cell.execution_count = None
        nbformat.write(nb, nb_path)

    return True, f"ok in {time.time() - t0:.0f}s ({', '.join(notes)})"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", help="notebooks and/or folders (searched recursively)")
    ap.add_argument("--changed", action="store_true", help="notebooks under tutorials/ changed vs --base (incl. uncommitted)")
    ap.add_argument("--stale", action="store_true", help="notebooks newer than their .md (or with no .md)")
    ap.add_argument("--base", default="origin/main", help="git ref for --changed (default origin/main)")
    ap.add_argument("--no-execute", dest="execute", action="store_false", help="don't re-run; convert existing outputs")
    ap.add_argument("--keep-outputs", action="store_true", help="don't clear outputs in the saved .ipynb")
    ap.add_argument("--timeout", type=int, default=6000, help="per-cell timeout in seconds (default 6000)")
    ap.add_argument("--kernel", default="python3", help="Jupyter kernel name (default python3)")
    ap.add_argument("--formats", default="md,py,html", help="comma list of md,py,html (default all)")
    ap.add_argument("--branch", default="main", help="branch used in raw.githubusercontent figure URLs (default main)")
    ap.add_argument("--strict", action="store_true", help="fail notebooks with missing/invalid front matter")
    ap.add_argument("--dry-run", action="store_true", help="list what would be converted and check front matter only")
    args = ap.parse_args(argv)

    root = repo_root(Path.cwd())
    nbs = find_notebooks(args.paths)
    if args.changed:
        nbs += changed_notebooks(root, args.base)
    if args.stale:
        nbs += stale_notebooks(root)
    nbs = sorted({p.resolve() for p in nbs})
    if not nbs:
        ap.print_usage()
        print("nb2md: no notebooks selected")
        return 1

    print(f"nb2md: {len(nbs)} notebook(s){' [dry run]' if args.dry_run else ''}"
          f"{'' if args.execute else ' [no execute]'}")
    failures = 0
    for nb in nbs:
        rel = nb.relative_to(root).as_posix()
        try:
            ok, msg = convert(nb, root, args)
        except Exception as e:  # keep going on unexpected errors
            ok, msg = False, f"{type(e).__name__}: {e}"
        failures += not ok
        print(f"  {'OK ' if ok else 'ERR'} {rel}: {msg}")
    print(f"nb2md: {len(nbs) - failures} succeeded, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
