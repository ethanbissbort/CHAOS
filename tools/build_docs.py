#!/usr/bin/env python3
"""Generate the Project CHAOS help site from README.md and docs/**/*.md.

    tools/build_docs.py                 regenerate both output shapes
    tools/build_docs.py --check         fail if the committed site is stale
    tools/build_docs.py --help          every option

Two shapes come out of one pass over the sources:

  src/chaos/web/docs/                       a directory site the platform
                                            serves at /ui/docs/ — and which
                                            also opens straight from disk
  src/chaos/web/docs/chaos-help-offline.html one file, everything inlined, no
                                            external reference of any kind

Both paths are options, not assumptions: pass --out and --single-file to put
them anywhere. Standard library only — no Markdown package, no site generator,
nothing to install before the help can be rebuilt.

The generated output is committed to the repository on purpose. The Windows
installer copies src/chaos/web verbatim and the Python package ships it, so
the help has to exist as files rather than as something someone remembered to
build. `--check` is what keeps that copy honest.
"""

from __future__ import annotations

import sys
from pathlib import Path

_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from docsite.build import main

if __name__ == "__main__":
    raise SystemExit(main())
