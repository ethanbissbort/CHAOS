"""Turn the repository's Markdown into the CHAOS help site.

Two output shapes, from one parse of one source set:

**Directory** (``src/chaos/web/docs/`` by default) — ``index.html`` plus
sibling pages and an ``assets/`` folder. Served by the platform under
``/ui/docs/``; also opens straight from disk, because nothing in it is
fetched: no ``fetch``, no ES modules, no absolute URLs.

**Single file** (``src/chaos/web/docs/chaos-help-offline.html`` by default) —
every page, the stylesheet, the runtime and the search index in one HTML
document with no external reference of any kind. This is the copy someone
opens when the platform is down and they need the troubleshooting page.

Both are deterministic: the same sources produce byte-identical output, with
no timestamps, no build host and no iteration over unordered collections.

LINK POLICY
    A Markdown destination is one of four things, and the difference matters:

    * a document in this set          → rewritten to the generated page,
                                        fragment validated against that
                                        document's real heading anchors;
    * a fragment on this page         → validated against this document;
    * a repository path that exists   → rendered as a monospace path, not a
      but is not in this set            link, because the help set does not
                                        contain it and a dead link in an
                                        outage is worse than a plain path;
    * anything else that is not an    → **a build failure**. A broken
      absolute URL                      cross-reference does not ship.
"""

from __future__ import annotations

import argparse
import base64
import filecmp
import json
import mimetypes
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import highlight as hl
from . import mdparse, runtime, theme
from .mdparse import esc_attr, esc_text

DEFAULT_SOURCES = ("README.md", "docs/**/*.md")
DEFAULT_OUT = "src/chaos/web/docs"
DEFAULT_SINGLE = "src/chaos/web/docs/chaos-help-offline.html"
TOKENS_CSS = "src/chaos/web/tokens.css"

SITE_TITLE = "Project CHAOS help"
SITE_SUBTITLE = "Central Homestead Automation and Operation System"

#: Reading order for documents whose names we recognise. Anything not listed
#: sorts after these, alphabetically by title, so adding, renaming or
#: restructuring a document changes where it appears — never whether it does.
READING_ORDER: tuple[str, ...] = (
    "readme",
    "index",
    "architecture",
    "deployment",
    "windows-deployment",
    "operations",
    "commissioning",
    "api",
    "network-and-trust-boundaries",
    "secondary-control-node",
    "water-control-narrative",
    "integration-findings",
)

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif"}
MAX_INLINE_IMAGE_BYTES = 2 * 1024 * 1024

#: The console's own mark. The scheme separator is percent-encoded so the file
#: contains no literal absolute URL; the data: URI decodes it before the SVG
#: parser ever sees the namespace.
FAVICON = (
    "data:image/svg+xml,%3Csvg xmlns='http%3A//www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
    "%3Crect width='32' height='32' rx='6' fill='%230e1a16'/%3E"
    "%3Cpath d='M6 18 16 8l10 10v8H6z' fill='none' stroke='%234ade80' stroke-width='2.4'"
    " stroke-linejoin='round'/%3E%3C/svg%3E"
)

EXTERNAL_NEEDLES = ("http://", "https://", "//cdn.", "fonts.googleapis", "integrity=")


class BuildError(RuntimeError):
    """Raised when the site cannot be generated correctly."""


# ---------------------------------------------------------------------------
# source documents
# ---------------------------------------------------------------------------


@dataclass
class Page:
    rel: str
    path: Path
    slug: str
    group: str
    group_order: int
    order_key: tuple[int, str]
    document: mdparse.Document
    title: str
    number: int = 0

    @property
    def anchors(self) -> set[str]:
        return {heading.slug for heading in self.document.headings}


@dataclass
class BuildReport:
    pages: list[Page] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    written: list[Path] = field(default_factory=list)
    sections: int = 0

    def note(self, message: str) -> None:
        """Something the author should look at. --strict turns these into failures."""
        if message not in self.warnings:
            self.warnings.append(message)

    def info(self, message: str) -> None:
        """Something worth printing that is not a problem."""
        if message not in self.notes:
            self.notes.append(message)


def _title_case(text: str) -> str:
    words = re.split(r"[-_\s]+", text.strip())
    return " ".join(word[:1].upper() + word[1:] for word in words if word)


def slug_for(rel: str) -> str:
    """Repo path → flat page slug.

    Flat, because the directory build is one folder of siblings: no depth to
    get wrong from ``file://``. A *sub*folder's README takes the folder's name
    (``docs/design-decisions/README.md`` → ``design-decisions``); the doc
    tree's own root keeps its filename, so ``docs/index.md`` is ``index`` and
    the repository README is ``readme``. Slugs never contain ``__`` — the
    single-file build splits page from anchor on exactly that separator.
    """
    stem = rel[:-3] if rel.lower().endswith(".md") else rel
    parts = [part for part in stem.split("/") if part]
    if len(parts) > 2 and parts[-1].lower() in ("readme", "index"):
        parts = parts[:-1]
    if len(parts) > 1 and parts[0] == "docs":
        parts = parts[1:]
    slug = re.sub(r"[^a-z0-9]+", "-", "-".join(parts).lower()).strip("-")
    return slug or "page"


def group_for(rel: str) -> tuple[str, int]:
    parts = rel.split("/")
    if len(parts) == 1:
        return "Overview", 1
    if parts[0] == "docs" and len(parts) == 2:
        return "Documentation", 8
    if parts[0] == "docs":
        return _title_case(parts[1]), 9
    return _title_case(parts[0]), 9


def order_key_for(rel: str, title: str) -> tuple[int, str]:
    stem = Path(rel).stem.lower()
    try:
        rank = READING_ORDER.index(stem)
    except ValueError:
        rank = len(READING_ORDER)
    return rank, title.lower()


def discover(repo_root: Path, patterns: Sequence[str], excludes: Sequence[str]) -> list[Path]:
    found: set[Path] = set()
    for pattern in patterns:
        for path in repo_root.glob(pattern):
            if path.is_file() and path.suffix.lower() == ".md":
                found.add(path.resolve())
    skipped: set[Path] = set()
    for pattern in excludes:
        for path in repo_root.glob(pattern):
            skipped.add(path.resolve())
    return sorted(found - skipped)


def load_pages(repo_root: Path, paths: Iterable[Path], report: BuildReport) -> list[Page]:
    pages: list[Page] = []
    taken: dict[str, int] = {}
    for path in paths:
        rel = path.relative_to(repo_root).as_posix()
        text = path.read_text(encoding="utf-8")
        document = mdparse.parse(text)
        for warning in document.warnings:
            report.note(f"{rel}: {warning}")
        title = document.title or _title_case(Path(rel).stem)
        base = slug_for(rel)
        seen = taken.get(base, 0)
        taken[base] = seen + 1
        slug = base if seen == 0 else f"{base}-{seen + 1}"
        group, group_order = group_for(rel)
        pages.append(
            Page(
                rel=rel,
                path=path,
                slug=slug,
                group=group,
                group_order=group_order,
                order_key=order_key_for(rel, title),
                document=document,
                title=title,
            )
        )
    plan_navigation(repo_root, pages, report)
    return pages


# ---------------------------------------------------------------------------
# navigation
# ---------------------------------------------------------------------------

_MD_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)")


def _raw_strings(blocks: Iterable[mdparse.Block]) -> Iterable[str]:
    for block in blocks:
        kind = block.kind
        if kind in ("para", "heading"):
            yield block.data["raw"]
        elif kind == "quote":
            yield from _raw_strings(block.data["children"])
        elif kind == "list":
            for item in block.data["items"]:
                yield from _raw_strings(item)
        elif kind == "table":
            yield from block.data["header"]
            for row in block.data["rows"]:
                yield from row


def _linked_docs(blocks: Iterable[mdparse.Block], repo_root: Path, from_rel: str) -> list[str]:
    """Repo paths linked from ``blocks``, in the order they are written."""
    found: list[str] = []
    for raw in _raw_strings(blocks):
        for match in _MD_LINK_RE.finditer(raw):
            dest = match.group(1)
            if dest.startswith(("#", "//")) or _SCHEME_PREFIX.match(dest):
                continue
            resolved = resolve_repo_path(repo_root, from_rel, _unquote(dest.partition("#")[0]))
            if resolved and resolved not in found:
                found.append(resolved)
    return found


def derive_manifest(repo_root: Path, index_page: Page, known: set[str]) -> list[tuple[str, list[str]]]:
    """Read the reading order out of the documentation's own index page.

    The docs carry an index that lists every document, grouped and ordered by
    the person who wrote them. That is a better reading order than anything
    this tool could infer, and it stays right when documents are added or
    renamed. The section is found by counting, not by matching a title: the
    top-level section that links to the most documents in the set wins, so
    renaming the heading does not break the navigation.
    """
    blocks = index_page.document.blocks
    sections: list[tuple[int, int]] = []
    for position, block in enumerate(blocks):
        if block.kind == "heading" and block.data["level"] == 2:
            sections.append((position, len(blocks)))
    for order, (start, _) in enumerate(sections):
        end = sections[order + 1][0] if order + 1 < len(sections) else len(blocks)
        sections[order] = (start, end)

    best: tuple[int, int, int] | None = None
    for start, end in sections:
        links = [rel for rel in _linked_docs(blocks[start:end], repo_root, index_page.rel) if rel in known]
        if links and (best is None or len(links) > best[0]):
            best = (len(links), start, end)
    if best is None or best[0] < 2:
        return []

    _, start, end = best
    groups: list[tuple[str, list[str]]] = []
    current: list[str] = []
    name = mdparse.strip_inline(blocks[start].data["raw"])
    cursor = start + 1
    while cursor < end:
        block = blocks[cursor]
        if block.kind == "heading" and block.data["level"] >= 3:
            if current:
                groups.append((name, current))
            name = block.data["text"]
            current = []
        else:
            for rel in _linked_docs([block], repo_root, index_page.rel):
                if rel in known and rel not in current:
                    current.append(rel)
        cursor += 1
    if current:
        groups.append((name, current))
    return groups


def plan_navigation(repo_root: Path, pages: list[Page], report: BuildReport) -> None:
    """Assign every page a group and a place in the reading order."""
    by_rel = {page.rel: page for page in pages}
    placed: set[str] = set()
    ordered: list[Page] = []

    def take(rel: str, group: str) -> None:
        page = by_rel.get(rel)
        if page is None or rel in placed:
            return
        page.group = group
        placed.add(rel)
        ordered.append(page)

    # The two entry points, in the order a reader meets them.
    index_rel = next(
        (rel for rel in ("docs/index.md", "docs/README.md") if rel in by_rel),
        None,
    )
    if index_rel:
        take(index_rel, "Overview")
    take("README.md", "Overview")

    manifest = derive_manifest(repo_root, by_rel[index_rel], set(by_rel)) if index_rel else []
    if manifest:
        report.info(
            f"navigation order taken from {index_rel} "
            f"({sum(len(rels) for _, rels in manifest)} documents in {len(manifest)} sections)"
        )
        for group, rels in manifest:
            for rel in rels:
                take(rel, group)

    # Anything the index does not mention still gets a home, grouped by folder
    # and ordered by the built-in reading order then by title.
    remainder = [page for page in pages if page.rel not in placed]
    for page in remainder:
        page.group, page.group_order = group_for(page.rel)
    remainder.sort(key=lambda page: (page.group_order, page.group, page.order_key, page.rel))
    ordered.extend(remainder)

    for number, page in enumerate(ordered, start=1):
        page.number = number
    pages[:] = ordered


# ---------------------------------------------------------------------------
# link resolution
# ---------------------------------------------------------------------------

_SCHEME_PREFIX = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
_HYPHEN_RUN = re.compile(r"-{2,}")
_LEADING_SECTION_NUMBER = re.compile(r"^\d+(?:-\d+)*-(?=[a-z])")


def resolve_repo_path(repo_root: Path, from_rel: str, raw: str) -> str | None:
    """Normalise a relative Markdown destination to an existing repo path."""
    if not raw:
        return from_rel
    base = Path(from_rel).parent
    candidate = os.path.normpath((base / raw).as_posix()).replace(os.sep, "/")
    if candidate.startswith(".."):
        return None
    absolute = repo_root / candidate
    if absolute.is_dir():
        for name in ("README.md", "readme.md", "index.md"):
            if (absolute / name).is_file():
                return f"{candidate}/{name}"
        return candidate
    if absolute.exists():
        return candidate
    if not raw.endswith(".md") and (repo_root / f"{candidate}.md").is_file():
        return f"{candidate}.md"
    return None


def match_anchor(fragment: str, anchors: set[str]) -> tuple[str | None, str | None]:
    """Resolve a hand-written fragment against a document's real anchors.

    Four tiers. Only the last two can surprise anyone, and both of them are
    reported so the source link can be tightened:

    1. **exact** — the normal case, silent.
    2. **hyphen runs collapsed** — ``## 6. Serviceability — a dark tile`` slugs
       to ``6-serviceability--a-dark-tile`` because the em dash leaves two
       spaces behind. An author who typed one hyphen meant that heading. Pure
       normalisation, so it is silent too.
    3. **unique prefix** — ``#9-alarms`` for ``## 9. Alarms — SDD 14``.
       Headings grow qualifiers over time; the number and the name still
       identify the section.
    4. **renumbered** — ``#8-status-summary`` for ``## 7. Status summary``.
       Section numbers get shuffled every time a document is reorganised; the
       name is the stable part.

    Tiers 3 and 4 fire only when **exactly one** heading matches. Two
    candidates is ambiguity, and ambiguity is a broken link, not a guess.

    Returns ``(anchor, warning)``. ``(None, None)`` means genuinely broken and
    the build fails.
    """
    if fragment in anchors:
        return fragment, None

    flat = _HYPHEN_RUN.sub("-", fragment)
    collapsed = {_HYPHEN_RUN.sub("-", anchor): anchor for anchor in sorted(anchors)}
    if flat in collapsed:
        return collapsed[flat], None

    candidates = sorted(anchor for key, anchor in collapsed.items() if key.startswith(flat + "-"))
    if len(candidates) == 1:
        return candidates[0], f"#{fragment} resolved to #{candidates[0]} by unique prefix"
    if len(candidates) > 1:
        return None, None

    unnumbered = _LEADING_SECTION_NUMBER.sub("", flat)
    if unnumbered != flat:
        renumbered = sorted(
            anchor for key, anchor in collapsed.items() if _LEADING_SECTION_NUMBER.sub("", key) == unnumbered
        )
        if len(renumbered) == 1:
            return renumbered[0], (
                f"#{fragment} resolved to #{renumbered[0]} — the section was renumbered; "
                "the link in the source still carries the old number"
            )
    return None, None


class Resolver:
    """Turns a Markdown destination into something the generated page can use."""

    def __init__(
        self,
        repo_root: Path,
        pages: Sequence[Page],
        mode: str,
        report: BuildReport | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.mode = mode
        self.by_rel = {page.rel: page for page in pages}
        self.errors: list[str] = []
        self.current: Page | None = None
        self.report = report

    # -- helpers ----------------------------------------------------------
    def page_href(self, page: Page, anchor: str = "") -> str:
        if self.mode == "single":
            return "#" + (f"{page.slug}__{anchor}" if anchor else page.slug)
        return f"{page.slug}.html" + (f"#{anchor}" if anchor else "")

    def self_href(self, anchor: str) -> str:
        assert self.current is not None
        if self.mode == "single":
            return f"#{self.current.slug}__{anchor}"
        return f"#{anchor}"

    def fail(self, message: str) -> None:
        assert self.current is not None
        self.errors.append(f"{self.current.rel}: {message}")

    def soft(self, message: str) -> None:
        assert self.current is not None
        if self.report is not None:
            self.report.note(f"{self.current.rel}: {message}")

    def anchor_in(self, page: Page, fragment: str, label: str, raw: str) -> str | None:
        matched, warning = match_anchor(fragment, page.anchors)
        if matched is None:
            where = "this document" if page is self.current else page.rel
            self.fail(f"link [{label}]({raw}) points at #{fragment}, which is not a heading in {where}")
            return None
        if warning:
            self.soft(f"link [{label}]({raw}): {warning}")
        return matched

    # -- the resolver ------------------------------------------------------
    def resolve(self, dest: str, label: str) -> mdparse.LinkTarget:
        assert self.current is not None, "resolver used outside a page"
        raw = (dest or "").strip()
        if not raw:
            self.fail(f"empty link destination for [{label}]")
            return mdparse.LinkTarget(href="#", kind="broken")

        if raw.startswith("#"):
            anchor = _unquote(raw[1:])
            if anchor:
                matched = self.anchor_in(self.current, anchor, label, raw)
                if matched is None:
                    return mdparse.LinkTarget(href="#", kind="broken")
                anchor = matched
            return mdparse.LinkTarget(href=self.self_href(anchor), kind="anchor")

        if raw.startswith("//") or _SCHEME_PREFIX.match(raw):
            return mdparse.LinkTarget(href=raw, kind="external")

        path_part, _, fragment = raw.partition("#")
        fragment = _unquote(fragment)
        target_rel = self._repo_path(_unquote(path_part))
        if target_rel is None:
            self.fail(f"link [{label}]({raw}) does not resolve to anything in the repository")
            return mdparse.LinkTarget(href="#", kind="broken")

        page = self.by_rel.get(target_rel)
        if page is None:
            # It exists in the repository but is not part of the help set.
            return mdparse.LinkTarget(href=target_rel, kind="repo", title=f"Repository path: {target_rel}")

        if fragment:
            matched = self.anchor_in(page, fragment, label, raw)
            if matched is None:
                return mdparse.LinkTarget(href="#", kind="broken")
            fragment = matched
        return mdparse.LinkTarget(href=self.page_href(page, fragment), kind="page")

    def resolve_image(self, dest: str, label: str) -> mdparse.LinkTarget:
        assert self.current is not None
        raw = (dest or "").strip()
        if raw.startswith("data:"):
            return mdparse.LinkTarget(href=raw, kind="external")
        if raw.startswith("//") or _SCHEME_PREFIX.match(raw):
            self.fail(f"image ![{label}]({raw}) is an absolute URL; the help set must carry its own images")
            return mdparse.LinkTarget(href="", kind="broken")
        rel = self._repo_path(_unquote(raw.partition("#")[0]))
        if rel is None:
            self.fail(f"image ![{label}]({raw}) does not resolve to a file in the repository")
            return mdparse.LinkTarget(href="", kind="broken")
        path = self.repo_root / rel
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            self.fail(f"image ![{label}]({raw}) is not a supported image type")
            return mdparse.LinkTarget(href="", kind="broken")
        payload = path.read_bytes()
        if len(payload) > MAX_INLINE_IMAGE_BYTES:
            self.fail(
                f"image ![{label}]({raw}) is {len(payload)} bytes; the limit for an inlined image "
                f"is {MAX_INLINE_IMAGE_BYTES}"
            )
            return mdparse.LinkTarget(href="", kind="broken")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        encoded = base64.b64encode(payload).decode("ascii")
        return mdparse.LinkTarget(href=f"data:{mime};base64,{encoded}", kind="external")

    def _repo_path(self, raw: str) -> str | None:
        assert self.current is not None
        return resolve_repo_path(self.repo_root, self.current.rel, raw)


def _unquote(text: str) -> str:
    from urllib.parse import unquote

    return unquote(text)


# ---------------------------------------------------------------------------
# search index
# ---------------------------------------------------------------------------


def build_index(pages: Sequence[Page]) -> list[dict[str, object]]:
    """One record per heading section, plus a lead record for every page.

    Section granularity is what makes a hit useful: the result jumps to the
    procedure, not to the top of a fifteen-thousand-word runbook.
    """
    records: list[dict[str, object]] = []
    for position, page in enumerate(pages):
        blocks = page.document.blocks
        lead: list[mdparse.Block] = []
        cursor = 0
        while cursor < len(blocks) and blocks[cursor].kind != "heading":
            lead.append(blocks[cursor])
            cursor += 1
        records.append(
            {
                "p": position,
                "a": "",
                "h": page.title,
                "l": 1,
                "t": _clean(mdparse.block_text(lead)),
            }
        )
        while cursor < len(blocks):
            heading = blocks[cursor]
            body: list[mdparse.Block] = []
            cursor += 1
            while cursor < len(blocks) and blocks[cursor].kind != "heading":
                body.append(blocks[cursor])
                cursor += 1
            records.append(
                {
                    "p": position,
                    "a": heading.data["slug"],
                    "h": heading.data["text"],
                    "l": heading.data["level"],
                    "t": _clean(mdparse.block_text(body)),
                }
            )
    return records


def _clean(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text).strip()


def dump_payload(pages: Sequence[Page], records: Sequence[dict[str, object]], mode: str) -> str:
    """Serialise the runtime configuration as a JS literal that is safe inline.

    ``<`` becomes ``\\u003c`` so no payload can close the surrounding
    ``<script>``, and ``://`` becomes ``\\u003a//`` so the file never contains
    a literal absolute URL. Both are decoded by the JavaScript parser, so the
    strings the runtime sees are exactly what the documents said.
    """
    payload = {
        "mode": mode,
        "title": SITE_TITLE,
        "pages": [
            {"s": page.slug, "t": page.title, "u": f"{page.slug}.html", "g": page.group} for page in pages
        ],
        "index": list(records),
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    text = text.replace("<", "\\u003c")
    text = re.sub(r"([A-Za-z][A-Za-z0-9+.\-]*)://", r"\1\\u003a//", text)
    for needle, replacement in (
        ("//cdn.", "//cdn\\u002e"),
        ("fonts.googleapis", "fonts\\u002egoogleapis"),
        ("integrity=", "integrity\\u003d"),
    ):
        text = text.replace(needle, replacement)
    return text


# ---------------------------------------------------------------------------
# HTML assembly
# ---------------------------------------------------------------------------

MARK_SVG = (
    '<svg viewBox="0 0 32 32" aria-hidden="true" focusable="false"><path d="M5 19 16 8l11 11v7H5z"/></svg>'
)
ICON_SEARCH = (
    '<svg class="icon" aria-hidden="true" focusable="false" viewBox="0 0 24 24">'
    '<circle cx="11" cy="11" r="6"/><path d="m20 20-4.3-4.3"/></svg>'
)
ICON_MENU = (
    '<svg class="icon" aria-hidden="true" focusable="false" viewBox="0 0 24 24">'
    '<path d="M4 7h16M4 12h16M4 17h16"/></svg>'
)
ICON_THEME = (
    '<svg class="icon" aria-hidden="true" focusable="false" viewBox="0 0 24 24">'
    '<circle cx="12" cy="12" r="4.2"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2'
    'M5.2 5.2l1.4 1.4M17.4 17.4l1.4 1.4M18.8 5.2l-1.4 1.4M6.6 17.4l-1.4 1.4"/></svg>'
)
ICON_PRINT = (
    '<svg class="icon" aria-hidden="true" focusable="false" viewBox="0 0 24 24">'
    '<path d="M7 9V4h10v5M7 18H5a1 1 0 0 1-1-1v-6a1 1 0 0 1 1-1h14a1 1 0 0 1 1 1v6a1 1 0 0 1-1 1h-2"/>'
    '<rect x="7" y="14" width="10" height="6" rx="1"/></svg>'
)


def nav_html(pages: Sequence[Page], resolver: Resolver, active: Page | None) -> str:
    out: list[str] = ['<nav class="site-nav" id="site-nav" aria-label="Documents">']
    current_group: str | None = None
    open_list = False
    for page in pages:
        if page.group != current_group:
            if open_list:
                out.append("</ul>")
            out.append(f"<h2>{esc_text(page.group)}</h2><ul>")
            current_group = page.group
            open_list = True
        current = ' aria-current="page"' if active is not None and page.slug == active.slug else ""
        out.append(
            f'<li><a href="{esc_attr(resolver.page_href(page))}" data-page="{esc_attr(page.slug)}"{current}>'
            f'<span class="nav-num">{page.number:02d}</span>'
            f"<span>{esc_text(page.title)}</span></a></li>"
        )
    if open_list:
        out.append("</ul>")
    out.append("</nav>")
    return "".join(out)


def toc_html(page: Page, resolver: Resolver, wrap: bool) -> str:
    headings = [heading for heading in page.document.headings if 2 <= heading.level <= 4]
    prefix = f"{page.slug}__" if resolver.mode == "single" else ""
    body: list[str] = []
    if headings:
        body.append("<h2>On this page</h2><ul>")
        for heading in headings:
            body.append(
                f'<li data-level="{heading.level}">'
                f'<a href="#{esc_attr(prefix + heading.slug)}" '
                f'data-anchor="{esc_attr(prefix + heading.slug)}">{esc_text(heading.text)}</a></li>'
            )
        body.append("</ul>")
    inner = "".join(body)
    if not wrap:
        return inner
    hidden = "" if page.number == 1 else " hidden"
    return f'<div class="toc-for" data-page="{esc_attr(page.slug)}"{hidden}>{inner}</div>'


def pager_html(pages: Sequence[Page], page: Page, resolver: Resolver) -> str:
    position = pages.index(page)
    previous = pages[position - 1] if position > 0 else None
    following = pages[position + 1] if position + 1 < len(pages) else None
    if previous is None and following is None:
        return ""
    parts = ['<nav class="doc-pager" aria-label="Reading order">']
    if previous is not None:
        parts.append(
            f'<a class="pager-prev" href="{esc_attr(resolver.page_href(previous))}" '
            f'data-page="{esc_attr(previous.slug)}">'
            f'<span class="pager-role">Previous</span>{esc_text(previous.title)}</a>'
        )
    if following is not None:
        parts.append(
            f'<a class="pager-next" href="{esc_attr(resolver.page_href(following))}" '
            f'data-page="{esc_attr(following.slug)}">'
            f'<span class="pager-role">Next</span>{esc_text(following.title)}</a>'
        )
    parts.append("</nav>")
    return "".join(parts)


def article_html(
    page: Page,
    pages: Sequence[Page],
    resolver: Resolver,
    report: BuildReport,
) -> str:
    resolver.current = page
    ctx = mdparse.InlineContext(
        resolve_link=resolver.resolve,
        resolve_image=resolver.resolve_image,
        link_defs=page.document.link_defs,
        footnote_defs=page.document.footnote_defs,
        id_prefix=f"{page.slug}__" if resolver.mode == "single" else "",
    )
    prefix = ctx.id_prefix
    rendered = mdparse.render(page.document, ctx, hl.highlight, id_prefix=prefix)
    for warning in rendered.warnings:
        report.note(f"{page.rel}: {warning}")
    hidden = ' hidden=""' if resolver.mode == "single" and page.number != 1 else ""
    crumbs = "".join(f"<span>{esc_text(part)}</span>" for part in (page.group, page.title) if part)
    return (
        f'<article class="doc-page" id="{esc_attr(page.slug)}" '
        f'data-page="{esc_attr(page.slug)}"{hidden}>'
        f'<p class="doc-breadcrumb">{crumbs}</p>'
        f'<p class="doc-source">{esc_text(page.rel)}</p>'
        f'<div class="doc-body">{rendered.html}</div>'
        f"{pager_html(pages, page, resolver)}"
        "</article>"
    )


def _head(title: str, style: str, inline: bool) -> str:
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        '<meta name="color-scheme" content="dark light">\n'
        f'<meta name="generator" content="tools/build_docs.py">\n'
        f"<title>{esc_text(title)}</title>\n"
        f'<link rel="icon" href="{FAVICON}">\n'
        + (f"<style>\n{style}\n</style>\n" if inline else f'<link rel="stylesheet" href="{style}">\n')
        + "<noscript><style>.doc-page[hidden]{display:block!important}"
        ".site-searchbtn{display:none}</style></noscript>\n"
        "</head>\n"
    )


def _chrome(pages: Sequence[Page], resolver: Resolver, active: Page | None, tocs: str) -> tuple[str, str]:
    home = resolver.page_href(pages[0]) if pages else "#"
    top = (
        '<a class="skip-link" href="#content">Skip to the documentation</a>\n'
        '<header class="site-topbar">\n'
        f'<button type="button" class="btn btn-icon site-navtoggle" id="nav-toggle" '
        f'aria-controls="site-nav" aria-expanded="false" title="Documents">{ICON_MENU}'
        '<span class="visually-hidden">Show the document list</span></button>\n'
        f'<a class="site-identity" href="{esc_attr(home)}">'
        f'<span class="site-mark">{MARK_SVG}</span>'
        f'<span class="site-titles"><h1>{esc_text(SITE_TITLE)}</h1>'
        f"<p>{esc_text(SITE_SUBTITLE)}</p></span></a>\n"
        f'<button type="button" class="site-searchbtn" id="search-open">'
        f"{ICON_SEARCH}<span>Search the documentation</span>"
        f'<span class="kbd-hint">/</span></button>\n'
        '<div class="site-tools">\n'
        f'<button type="button" class="btn btn-sm" id="theme-toggle">{ICON_THEME}'
        '<span class="theme-label">System</span></button>\n'
        f'<button type="button" class="btn btn-icon" id="print-page" onclick="window.print()" '
        f'title="Print this page">{ICON_PRINT}'
        '<span class="visually-hidden">Print this page</span></button>\n'
        "</div>\n</header>\n"
        '<div class="site-shell">\n'
        + nav_html(pages, resolver, active)
        + '\n<main class="site-main" id="content" tabindex="-1">\n'
    )
    bottom = (
        "\n</main>\n"
        f'<aside class="site-toc" aria-label="On this page">{tocs}</aside>\n'
        "</div>\n"
        '<div class="site-search" id="search-overlay" hidden role="dialog" aria-modal="true"'
        ' aria-label="Search the documentation">\n'
        '<div class="site-search-panel">\n'
        '<div class="site-search-head">\n'
        f"{ICON_SEARCH}\n"
        '<input type="search" id="search-input" autocomplete="off" spellcheck="false"'
        ' role="combobox" aria-expanded="true" aria-controls="search-results"'
        ' placeholder="Search every page — try black start, dead letter, interlock">\n'
        '<button type="button" class="btn btn-sm" id="search-close">Close</button>\n'
        "</div>\n"
        '<p class="site-search-status" id="search-status" role="status">'
        "Type to search every page of this help set.</p>\n"
        '<ul class="site-results" id="search-results" role="listbox"'
        ' aria-label="Search results"></ul>\n'
        '<div class="site-search-foot">'
        '<span><span class="kbd-hint">&#8593;</span> <span class="kbd-hint">&#8595;</span> move</span>'
        '<span><span class="kbd-hint">Enter</span> open</span>'
        '<span><span class="kbd-hint">Esc</span> close</span>'
        '<span><span class="kbd-hint">/</span> search from anywhere</span>'
        "</div>\n"
        "</div>\n</div>\n"
    )
    return top, bottom


# ---------------------------------------------------------------------------
# writers
# ---------------------------------------------------------------------------


def _stylesheet(repo_root: Path, report: BuildReport) -> str:
    tokens_path = repo_root / TOKENS_CSS
    if not tokens_path.is_file():
        raise BuildError(
            f"{TOKENS_CSS} is missing. The help site is styled from the operator console's "
            "design tokens and will not be generated without them."
        )
    tokens = tokens_path.read_text(encoding="utf-8")
    overrides, warnings = theme.derive_theme_overrides(tokens)
    for warning in warnings:
        report.note(warning)
    banner = (
        "/* Generated by tools/build_docs.py — do not edit.\n"
        f"   Design tokens below are a verbatim copy of {TOKENS_CSS}. */\n"
    )
    return banner + tokens.rstrip() + "\n\n" + overrides + theme.SITE_CSS


def _guard(text: str, where: str) -> str:
    for needle in EXTERNAL_NEEDLES:
        if needle in text:
            position = text.index(needle)
            excerpt = text[max(0, position - 60) : position + 60].replace("\n", " ")
            raise BuildError(
                f"{where} contains an external reference ({needle!r}) — the help site must work "
                f"with no network at all. Near: …{excerpt}…"
            )
    return text


def _commit(
    files: Sequence[tuple[Path, str]],
    report: BuildReport,
    clean: Path | None = None,
    preserve: Sequence[Path] = (),
) -> None:
    """Check everything, then write everything.

    A build either produces a whole site or leaves the previous one alone. The
    half-written alternative is the worst outcome: a help set that opens, looks
    fine, and is missing the page someone came for.
    """
    for path, text in files:
        _guard(text, path.name)
    if clean is not None:
        keep = {path.resolve() for path in preserve} | {path.resolve() for path, _ in files}
        _clean_output(clean, keep)
    for path, text in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        report.written.append(path)


def _clean_output(out_dir: Path, keep: set[Path]) -> None:
    """Remove the previous directory build so a deleted document leaves no page.

    ``keep`` is what this invocation is responsible for, plus the single-file
    build's target. Without it, ``--no-single-file`` would quietly delete the
    committed offline copy — the one file that has to exist during an outage —
    because it lives in the same folder and ends in ``.html``.
    """
    if not out_dir.exists():
        return
    assets = out_dir / "assets"
    if assets.is_dir():
        shutil.rmtree(assets)
    for path in sorted(out_dir.glob("*.html")):
        if path.resolve() not in keep:
            path.unlink()


def write_directory(
    repo_root: Path,
    out_dir: Path,
    pages: Sequence[Page],
    stylesheet: str,
    report: BuildReport,
    preserve: Sequence[Path] = (),
) -> None:
    resolver = Resolver(repo_root, pages, "dir", report)
    records = build_index(pages)
    report.sections = len(records)

    files: list[tuple[Path, str]] = [
        (out_dir / "assets" / "docs.css", stylesheet),
        (out_dir / "assets" / "docs.js", runtime.SITE_JS.lstrip("\n")),
        (
            out_dir / "assets" / "search-index.js",
            "/* Generated by tools/build_docs.py — do not edit. */\n"
            "window.__CHAOS_DOCS__ = " + dump_payload(pages, records, "dir") + ";\n",
        ),
    ]

    for page in pages:
        body = article_html(page, pages, resolver, report)
        top, bottom = _chrome(pages, resolver, page, toc_html(page, resolver, wrap=False))
        html = (
            _head(f"{page.title} — {SITE_TITLE}", "assets/docs.css", inline=False)
            + f'<body data-page="{esc_attr(page.slug)}">\n'
            + top
            + body
            + bottom
            + '<script src="assets/search-index.js"></script>\n'
            '<script src="assets/docs.js"></script>\n'
            "</body>\n</html>\n"
        )
        name = "index.html" if page.number == 1 else f"{page.slug}.html"
        files.append((out_dir / name, html))
        if page.number == 1 and page.slug != "index":
            files.append((out_dir / f"{page.slug}.html", html))

    _raise_if_broken(resolver)
    _commit(files, report, clean=out_dir, preserve=preserve)


def write_single_file(
    repo_root: Path,
    out_path: Path,
    pages: Sequence[Page],
    stylesheet: str,
    report: BuildReport,
) -> None:
    resolver = Resolver(repo_root, pages, "single", report)
    records = build_index(pages)

    articles = "\n".join(article_html(page, pages, resolver, report) for page in pages)
    tocs = "".join(toc_html(page, resolver, wrap=True) for page in pages)
    top, bottom = _chrome(pages, resolver, pages[0] if pages else None, tocs)
    payload = dump_payload(pages, records, "single")
    html = (
        _head(SITE_TITLE, stylesheet, inline=True)
        + "<body>\n"
        + top
        + articles
        + bottom
        + f"<script>\nwindow.__CHAOS_DOCS__ = {payload};\n</script>\n"
        + f"<script>\n{runtime.SITE_JS.strip()}\n</script>\n"
        + "</body>\n</html>\n"
    )
    _raise_if_broken(resolver)
    _commit([(out_path, html)], report)


def _raise_if_broken(resolver: Resolver) -> None:
    if resolver.errors:
        listing = "\n".join(f"  - {message}" for message in sorted(set(resolver.errors)))
        raise BuildError(
            "the documentation has broken cross-references, so the site was not written:\n" + listing
        )


# ---------------------------------------------------------------------------
# top level
# ---------------------------------------------------------------------------


def build_site(
    repo_root: Path,
    out_dir: Path | None,
    single_file: Path | None,
    sources: Sequence[str] = DEFAULT_SOURCES,
    excludes: Sequence[str] = (),
    strict: bool = False,
    preserve: Sequence[Path] = (),
) -> BuildReport:
    report = BuildReport()
    paths = discover(repo_root, sources, excludes)
    if not paths:
        raise BuildError("no Markdown sources matched " + ", ".join(sources) + f" under {repo_root}")
    pages = load_pages(repo_root, paths, report)
    report.pages = list(pages)
    stylesheet = _stylesheet(repo_root, report)

    if out_dir is not None:
        write_directory(repo_root, out_dir, pages, stylesheet, report, preserve)
    if single_file is not None:
        write_single_file(repo_root, single_file, pages, stylesheet, report)
    if not report.sections:
        report.sections = len(build_index(pages))

    if strict and report.warnings:
        listing = "\n".join(f"  - {warning}" for warning in report.warnings)
        raise BuildError("--strict: the build produced warnings:\n" + listing)
    return report


def check_up_to_date(
    repo_root: Path,
    out_dir: Path,
    single_file: Path | None,
    sources: Sequence[str] = DEFAULT_SOURCES,
    excludes: Sequence[str] = (),
) -> list[str]:
    """Return the paths whose committed content no longer matches the sources."""
    stale: list[str] = []
    with tempfile.TemporaryDirectory() as raw:
        scratch = Path(raw)
        fresh_dir = scratch / "site"
        fresh_single = scratch / "single.html" if single_file is not None else None
        build_site(repo_root, fresh_dir, fresh_single, sources, excludes)

        expected = {path.relative_to(fresh_dir).as_posix() for path in fresh_dir.rglob("*") if path.is_file()}
        present = {path.relative_to(out_dir).as_posix() for path in out_dir.rglob("*") if path.is_file()}
        if single_file is not None:
            try:
                present.discard(single_file.resolve().relative_to(out_dir.resolve()).as_posix())
            except ValueError:
                pass
        for name in sorted(expected | present):
            left = fresh_dir / name
            right = out_dir / name
            if not right.is_file():
                stale.append(f"missing: {name}")
            elif not left.is_file():
                stale.append(f"unexpected: {name}")
            elif not filecmp.cmp(left, right, shallow=False):
                stale.append(f"out of date: {name}")
        if single_file is not None and fresh_single is not None:
            if not single_file.is_file():
                stale.append(f"missing: {single_file}")
            elif not filecmp.cmp(fresh_single, single_file, shallow=False):
                stale.append(f"out of date: {single_file}")
    return stale


def _human(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.2f} MiB"
    return f"{size / 1024:.1f} KiB"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build_docs",
        description="Generate the Project CHAOS help site from the repository Markdown.",
    )
    here = Path(__file__).resolve()
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=here.parents[2],
        help="repository root (default: the repository this tool lives in)",
    )
    parser.add_argument("--out", type=Path, default=None, help=f"directory site (default: {DEFAULT_OUT})")
    parser.add_argument(
        "--single-file",
        type=Path,
        default=None,
        help=f"single-file build (default: {DEFAULT_SINGLE})",
    )
    parser.add_argument(
        "--source",
        action="append",
        default=None,
        metavar="GLOB",
        help="source glob, repeatable (default: " + " ".join(DEFAULT_SOURCES) + ")",
    )
    parser.add_argument(
        "--exclude", action="append", default=[], metavar="GLOB", help="glob to leave out, repeatable"
    )
    parser.add_argument("--no-directory", action="store_true", help="skip the directory build")
    parser.add_argument("--no-single-file", action="store_true", help="skip the single-file build")
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures")
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; fail if the committed output no longer matches the sources",
    )
    parser.add_argument("--quiet", action="store_true", help="only report problems")
    args = parser.parse_args(argv)

    repo_root = args.repo_root.resolve()
    out_dir = None if args.no_directory else (args.out or repo_root / DEFAULT_OUT)
    # Where the single file lives, whether or not this run rebuilds it: the
    # directory clean-up has to know not to delete it.
    single_path = args.single_file or repo_root / DEFAULT_SINGLE
    single = None if args.no_single_file else single_path
    sources = tuple(args.source) if args.source else DEFAULT_SOURCES

    try:
        if args.check:
            if out_dir is None:
                raise BuildError("--check needs the directory build")
            stale = check_up_to_date(
                repo_root, Path(out_dir), Path(single) if single else None, sources, tuple(args.exclude)
            )
            if stale:
                print("The generated help site is out of date:", file=sys.stderr)
                for item in stale:
                    print(f"  - {item}", file=sys.stderr)
                return 1
            if not args.quiet:
                print("Help site is up to date with the documentation.")
            return 0

        report = build_site(
            repo_root,
            Path(out_dir) if out_dir else None,
            Path(single) if single else None,
            sources,
            tuple(args.exclude),
            strict=args.strict,
            preserve=(Path(single_path),),
        )
    except BuildError as error:
        print(f"build_docs: {error}", file=sys.stderr)
        return 1

    if report.warnings and not args.quiet:
        print(f"{len(report.warnings)} warning(s):", file=sys.stderr)
        for warning in report.warnings:
            print(f"  - {warning}", file=sys.stderr)

    if not args.quiet:
        for note in report.notes:
            print(note)
        print(f"{len(report.pages)} documents, {report.sections} indexed sections")
        for page in report.pages:
            print(f"  {page.number:02d}  {page.slug:<44} {page.rel}")
        if out_dir:
            total = sum(path.stat().st_size for path in Path(out_dir).rglob("*") if path.is_file())
            print(f"\ndirectory site : {out_dir}  ({_human(total)} total)")
        if single:
            print(f"single file    : {single}  ({_human(Path(single).stat().st_size)})")
    return 0
