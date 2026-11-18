---
title: About this site
---

# About this site

This site is generated from the [NEON-Data-Skills](https://github.com/NEONScience/NEON-Data-Skills)
GitHub repository. The official, published versions of these tutorials are on the
[NEON Learning Hub](https://www.neonscience.org/resources/learning-hub/tutorials).

## How it is built

1. Tutorials are written as R Markdown (`.Rmd`) or Jupyter notebooks (`.ipynb`) and rendered to
   Markdown (`.md`) with the scripts in `processing_code/`.
2. `mkdocs/build_docs.py` reads the rendered `.md` files and their YAML front matter, normalizes
   the keywords using the vocabularies in `mkdocs/vocab/`, merges curated metadata from
   `mkdocs/tutorial_meta.csv` (theme, task type, skill level), and generates the browse pages.
3. [MkDocs Material](https://squidfunk.github.io/mkdocs-material/) builds the website.

See `mkdocs/README.md` in the repository for details.
