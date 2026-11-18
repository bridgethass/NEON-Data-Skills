# NEON Data Skills — MkDocs site

A browsable, searchable website for the tutorials in `tutorials/`, built with
[MkDocs Material](https://squidfunk.github.io/mkdocs-material/). It adds:

- **Find a Tutorial**: a filter page with facets for Language/Tool, Topic, System (AOP/OS/IS/Combined),
  Data Product (nested under System), Workflow/Task and Skill Level. Filtered views can be linked.
- **Browse pages** for each facet and for Themes, plus a **Tag Index** of topics.
- **Full-text search**, and a metadata card with facet chips at the top of every tutorial.

> The tutorial `.md` files are **not modified**. On every push to `main`, CircleCI sends changed
> `tutorials/**/*.md` files to the neonscience.org (Drupal) import. This site reads those files
> **read-only** and writes a generated copy to `mkdocs/docs/`, which is gitignored.

## Quick start

```bash
# one-time: create an environment (any Python 3.10+; uv shown here)
uv venv mkdocs/.venv
uv pip install --python mkdocs/.venv -r mkdocs/requirements.txt
# activate:  source mkdocs/.venv/Scripts/activate  (Windows Git Bash)  |  source mkdocs/.venv/bin/activate  (mac/Linux)

python mkdocs/build_docs.py              # generate mkdocs/docs/ and print the metadata report
mkdocs serve -f mkdocs/mkdocs.yml        # preview at http://127.0.0.1:8000
```

Re-run `build_docs.py` after editing tutorials, vocab files or `tutorial_meta.csv`.
`mkdocs serve` will then reload on its own.

## Files

| Path | What it is | Edit? |
|---|---|---|
| `build_docs.py` | Generator: tutorials + metadata → `docs/` | code |
| `mkdocs.yml` | Site config (theme, plugins) | yes |
| `hooks.py` | Loads the generated navigation (`docs/.nav.yml`) | rarely |
| `tutorial_meta.csv` | **Curated metadata per tutorial**: theme, subtheme, task types, skill level, system override | **yes** |
| `vocab/topics.yml` | Canonical topics + aliases (maps messy front-matter `topics:` values) | yes |
| `vocab/languages.yml` | Canonical languages/tools + aliases + folder defaults | yes |
| `vocab/task_types.yml`, `vocab/skill_levels.yml`, `vocab/systems.yml` | Facet vocabularies | yes |
| `vocab/data_products.yml` | Product code → name + system, from the NEON API | regenerate |
| `vocab/exclude.yml` | Paths under `tutorials/` left out of the site | yes |
| `pages/` | Hand-written pages (home, about, tags, filter page), CSS, JS | yes |
| `docs/`, `site/` | Generated output | **no** (gitignored) |

## Where each facet comes from

| Facet | Source |
|---|---|
| Language / Tool | Front matter `languagesTool` / `languageTool` / `languageTools`, normalized with `vocab/languages.yml`. If empty, inferred from the top folder. |
| Topic | Front matter `topics` + `subtopics`, normalized with `vocab/topics.yml`. These become the Material **tags**. |
| Data Product | Front matter `dataProduct` / `dataProducts` (any `DPx.xxxxx` IDs found). |
| System | Derived from the data products' NEON science team (AOP → AOP; TOS/AOS → OS; TIS/AIS → IS; more than one → Combined; none → General). `system_override` in the CSV wins. |
| Workflow / Task, Skill Level, Theme | `tutorial_meta.csv` |

### Curating `tutorial_meta.csv`

- Open it in Excel or Google Sheets. `task_types` takes several slugs separated by `;`, e.g. `download-access;explore-visualize`.
- The first values were **guessed**. Themes come from `tutorials/TOC-by-theme.md`, system overrides from
  `TOC-by-system.md`, and task and skill from keywords. Put anything (e.g. `x` or your initials) in `reviewed`
  once you've checked a row. The build report counts reviewed rows.
- `python mkdocs/build_docs.py --init-meta` adds rows for new tutorials and never overwrites existing values.
  Rows whose file no longer exists are marked `ORPHAN` in `notes`. Rows follow moved files by `sync_id`.
- Later, once we've confirmed the Drupal import ignores unknown keys, these fields can move into each tutorial's
  front matter (Rmd/ipynb header).

### Build report

Each build prints:
- front matter that is missing or won't parse;
- **unknown topics/languages** (add them as aliases in `vocab/`);
- unrecognized data product IDs;
- tutorials missing from the CSV;
- missing curated values.

Add `--stale` to list `.md` files whose `.Rmd`/`.ipynb` source was committed more recently.

Refresh product names and systems with `python mkdocs/build_docs.py --refresh-products`. This needs internet access.

## Rendering tutorials (unchanged workflow)

- **R Markdown:** keep using `processing_code/01knit-RMD-2-MD_NDSRepo.R`. The site converts knitr's indented code
  blocks into highlighted ```` ```r ```` fences at build time, so the knit output doesn't need to change.
- **Jupyter notebooks:** use `processing_code/nb2md.py`, the Python replacement for
  `02process-ipynb-2-MD_NDSRepo.R`. It produces the same `.md` / `.py` / `.html` outputs and raw-GitHub figure
  links. Run it from an environment that has Jupyter and the notebook's packages:

  ```bash
  python processing_code/nb2md.py tutorials/Python/AOP/Lidar/intro-lidar     # all notebooks in a folder (re-executes)
  python processing_code/nb2md.py path/to/nb.ipynb --no-execute            # use outputs already saved in the notebook
  python processing_code/nb2md.py --changed                                # notebooks changed vs origin/main
  python processing_code/nb2md.py --stale --dry-run                        # which notebooks' .md is out of date?
  ```

  It warns if the first cell isn't YAML front matter (`--strict` makes that an error), and flags very large outputs.

## Deploying a preview (fork)

`.github/workflows/mkdocs.yml` builds and deploys on pushes to the `mkdocs-site` branch, or when run by hand.
It uses a sparse checkout, so the multi-GB data files are never downloaded.

1. Fork the repo (e.g. `bridgethass/NEON-Data-Skills`) and push the `mkdocs-site` branch to the fork.
2. In the fork, set **Settings → Pages → Source** to **GitHub Actions**.
3. Under **Settings → Environments → github-pages → Deployment branches**, allow `mkdocs-site`.
4. Check that `site_url` in `mkdocs.yml` matches the Pages URL.

## Known issues / next steps

- About 10 tutorial `.md` files have tab characters in their front matter. The site tolerates this, but the
  source should be fixed. `Python/NEON-general/NEON-download-explore/NEON-download-explore.md` has no front matter
  at all, and `calc_ndvi_extract_spectra.md` lists `DP3.3006.002` (should be `DP3.30006.002`).
- The three "Tabbed" pages are 2–10 MB because their images are embedded inline as base64.
- Material for MkDocs is in maintenance mode, with end of life on 2027-05-05. Its successor, Zensical, reads
  `mkdocs.yml` directly, so moving later should be low-effort.
