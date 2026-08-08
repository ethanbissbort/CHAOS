"""The generated help site: every source present, every link real, nothing external.

What these tests are protecting, in order of how much it would hurt to lose it:

1. **The single-file build opens from ``file://``.** That is the copy someone
   double-clicks when the platform is down and they need the troubleshooting
   page. One ``<script src>`` or one ``fetch`` and it is a blank page at the
   worst possible moment.
2. **No external references anywhere.** Same rule the operator console lives
   under (``test_overview_api.py::test_ui_has_no_external_network_references``),
   and the generated site sits inside ``src/chaos/web/`` where that test also
   scans it.
3. **Broken cross-references fail the build.** A dead link in an outage sends
   someone looking for a page that does not exist.
4. **The committed output matches the documentation.** The owner does not run
   build commands, the Windows installer copies ``src/chaos/web`` verbatim, and
   the Python package ships it — so the site is committed, and this suite is
   what stops it going stale.
"""

from __future__ import annotations

import html
import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = REPO_ROOT / "tools"
OUT_DIR = REPO_ROOT / "src" / "chaos" / "web" / "docs"
SINGLE_FILE = OUT_DIR / "chaos-help-offline.html"


def _load_generator():
    if str(TOOLS_DIR) not in sys.path:
        sys.path.insert(0, str(TOOLS_DIR))
    spec = importlib.util.find_spec("docsite.build")
    if spec is None:  # pragma: no cover - the tool is part of the repository
        pytest.skip("tools/docsite is not importable")
    import docsite.build as build_module

    return build_module


build = _load_generator()


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def sources() -> list[Path]:
    found = build.discover(REPO_ROOT, build.DEFAULT_SOURCES, ())
    assert found, "no Markdown sources were discovered"
    return found


@pytest.fixture(scope="module")
def site(tmp_path_factory) -> dict[str, object]:
    """One real build into a scratch directory, shared by the whole module."""
    scratch = tmp_path_factory.mktemp("docsite")
    out_dir = scratch / "site"
    single = scratch / "chaos-help-offline.html"
    report = build.build_site(REPO_ROOT, out_dir, single)
    return {
        "report": report,
        "out_dir": out_dir,
        "single": single,
        "pages": report.pages,
        "single_html": single.read_text(encoding="utf-8"),
    }


def _html_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.html"))


# ---------------------------------------------------------------------------
# coverage: every source document reaches the output
# ---------------------------------------------------------------------------


def test_every_source_document_becomes_a_page(site, sources):
    rels = {path.relative_to(REPO_ROOT).as_posix() for path in sources}
    produced = {page.rel for page in site["pages"]}
    assert produced == rels, f"documents missing from the site: {sorted(rels - produced)}"


def test_every_page_has_a_file_in_the_directory_build(site):
    out_dir: Path = site["out_dir"]
    for page in site["pages"]:
        name = "index.html" if page.number == 1 else f"{page.slug}.html"
        assert (out_dir / name).is_file(), f"{page.rel} produced no {name}"
    assert (out_dir / "index.html").is_file(), "the directory build has no front page"


def test_every_page_body_reaches_the_single_file(site):
    single_html: str = site["single_html"]
    for page in site["pages"]:
        marker = f'data-page="{page.slug}"'
        assert marker in single_html, f"{page.rel} is missing from the single-file build"
    articles = single_html.count('<article class="doc-page"')
    assert articles == len(site["pages"])


def test_document_titles_and_headings_survive(site):
    out_dir: Path = site["out_dir"]
    for page in site["pages"]:
        name = "index.html" if page.number == 1 else f"{page.slug}.html"
        text = (out_dir / name).read_text(encoding="utf-8")
        assert html.escape(page.title, quote=False) in text or page.title in text
        for heading in page.document.headings[:12]:
            assert f'id="{heading.slug}"' in text, f"{page.rel}: heading {heading.text!r} lost its anchor"


# ---------------------------------------------------------------------------
# links
# ---------------------------------------------------------------------------

_HREF_RE = re.compile(r'href="([^"]*)"')
_ID_RE = re.compile(r'\sid="([^"]*)"')


def test_every_internal_link_in_the_directory_build_resolves(site):
    out_dir: Path = site["out_dir"]
    files = _html_files(out_dir)
    ids: dict[str, set[str]] = {}
    for path in files:
        ids[path.name] = set(_ID_RE.findall(path.read_text(encoding="utf-8")))

    broken: list[str] = []
    for path in files:
        if path.name == SINGLE_FILE.name:
            continue
        text = path.read_text(encoding="utf-8")
        for href in _HREF_RE.findall(text):
            if href.startswith(("mailto:", "data:")) or "&#58;//" in href:
                continue
            target, _, fragment = href.partition("#")
            if target:
                if not (path.parent / target).exists():
                    broken.append(f"{path.name} -> {href} (no such file)")
                    continue
                pool = ids.get(Path(target).name, set())
            else:
                pool = ids[path.name]
            if fragment and fragment not in pool:
                broken.append(f"{path.name} -> {href} (no such anchor)")
    assert not broken, "broken links in the directory build:\n" + "\n".join(broken)


def test_every_link_in_the_single_file_is_same_document(site):
    single_html: str = site["single_html"]
    ids = set(_ID_RE.findall(single_html))
    broken: list[str] = []
    for href in _HREF_RE.findall(single_html):
        if href.startswith(("mailto:", "data:")) or "&#58;//" in href:
            continue
        assert href.startswith("#"), f"single-file build links out to {href!r}"
        fragment = href[1:]
        if fragment and fragment not in ids:
            broken.append(href)
    assert not broken, "dangling anchors in the single-file build:\n" + "\n".join(broken)


def test_a_broken_cross_reference_fails_the_build(tmp_path):
    """The requirement, stated as a test: a dead link does not ship."""
    fake = tmp_path / "repo"
    (fake / "docs").mkdir(parents=True)
    (fake / "src" / "chaos" / "web").mkdir(parents=True)
    (fake / "src" / "chaos" / "web" / "tokens.css").write_text(
        ":root { --accent: #4ade80; }\n@media (prefers-color-scheme: light) { :root { --accent: #096240; } }\n",
        encoding="utf-8",
    )
    (fake / "README.md").write_text("# Home\n\nSee [gone](docs/gone.md).\n", encoding="utf-8")
    (fake / "docs" / "real.md").write_text("# Real\n", encoding="utf-8")

    with pytest.raises(build.BuildError) as error:
        build.build_site(fake, tmp_path / "out", None)
    assert "gone.md" in str(error.value)
    assert not (tmp_path / "out" / "index.html").exists(), "a failed build must not leave a page behind"


def test_a_dangling_fragment_fails_the_build(tmp_path):
    fake = tmp_path / "repo"
    (fake / "docs").mkdir(parents=True)
    (fake / "src" / "chaos" / "web").mkdir(parents=True)
    (fake / "src" / "chaos" / "web" / "tokens.css").write_text(
        ":root { --accent: #4ade80; }\n", encoding="utf-8"
    )
    (fake / "README.md").write_text("# Home\n\n[nope](docs/real.md#not-a-heading)\n", encoding="utf-8")
    (fake / "docs" / "real.md").write_text("# Real\n\n## Actual heading\n", encoding="utf-8")

    with pytest.raises(build.BuildError) as error:
        build.build_site(fake, tmp_path / "out", None)
    assert "not-a-heading" in str(error.value)


def test_a_repository_path_outside_the_help_set_is_not_a_link(site):
    """LICENSE and friends exist but are not pages: they render as paths, not dead links."""
    out_dir: Path = site["out_dir"]
    blob = "".join(path.read_text(encoding="utf-8") for path in _html_files(out_dir))
    if "repo-ref" in blob:
        assert 'class="repo-ref"' in blob
        assert 'href="LICENSE"' not in blob


# ---------------------------------------------------------------------------
# offline guarantees
# ---------------------------------------------------------------------------

#: Mirrors tests/test_overview_api.py. The SVG namespace is an identifier, not
#: a URL, but the generator does not even emit that: it percent-encodes the
#: scheme separator inside its favicon, so the exemption is unused here and the
#: list is kept only so the two tests obviously encode the same rule.
SAFE_NAMESPACE_STRINGS = (
    "http://www.w3.org/2000/svg",
    "http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg",
)
EXTERNAL_NEEDLES = ("http://", "https://", "//cdn.", "fonts.googleapis", "integrity=")


def test_no_generated_asset_references_an_external_host(site):
    out_dir: Path = site["out_dir"]
    assets = [path for path in out_dir.rglob("*") if path.suffix in (".html", ".js", ".css")]
    assert assets, "the build produced no assets"
    for path in assets:
        text = path.read_text(encoding="utf-8")
        for safe in SAFE_NAMESPACE_STRINGS:
            text = text.replace(safe, "")
        for needle in EXTERNAL_NEEDLES:
            assert needle not in text, f"{path.name} references an external resource: {needle}"


def test_committed_output_obeys_the_console_rule():
    """The site lives inside src/chaos/web, so the console's own rule covers it."""
    if not OUT_DIR.is_dir():
        pytest.skip("the site has not been generated into the repository yet")
    for path in OUT_DIR.rglob("*"):
        if path.suffix not in (".html", ".js", ".css"):
            continue
        text = path.read_text(encoding="utf-8")
        for safe in SAFE_NAMESPACE_STRINGS:
            text = text.replace(safe, "")
        for needle in EXTERNAL_NEEDLES:
            assert needle not in text, f"{path.name} references an external resource: {needle}"


def test_single_file_has_no_external_reference_of_any_kind(site):
    text: str = site["single_html"]
    for needle in EXTERNAL_NEEDLES:
        assert needle not in text, f"the single-file build references {needle}"
    assert '<link rel="stylesheet"' not in text, "the single-file build loads a stylesheet"
    assert "<script src=" not in text, "the single-file build loads a script"
    assert 'type="module"' not in text, "ES modules do not load from file://"
    for src in re.findall(r'<img[^>]*\ssrc="([^"]*)"', text):
        assert src.startswith("data:"), f"the single-file build fetches an image: {src}"


def test_single_file_never_fetches_anything(site):
    text: str = site["single_html"]
    for forbidden in (
        "fetch(",
        "XMLHttpRequest",
        "import(",
        "importScripts",
        "WebSocket",
        "navigator.sendBeacon",
    ):
        assert forbidden not in text, f"the single-file build calls {forbidden}"


def test_single_file_is_one_self_contained_document(site):
    text: str = site["single_html"]
    assert text.startswith("<!DOCTYPE html>")
    assert text.rstrip().endswith("</html>")
    assert "<style>" in text and "window.__CHAOS_DOCS__" in text
    # Nothing outside the document: every href is a fragment (checked
    # elsewhere) and there is no <base>, <iframe>, <object> or <embed>.
    for tag in ("<base", "<iframe", "<object", "<embed", "<frame"):
        assert tag not in text.lower(), f"the single-file build contains {tag}"


def test_directory_build_also_opens_from_file_urls(site):
    """The served build must not depend on being served."""
    front = (site["out_dir"] / "index.html").read_text(encoding="utf-8")
    assert 'type="module"' not in front, "ES modules are blocked from file://"
    assert "fetch(" not in front
    for href in _HREF_RE.findall(front) + re.findall(r'src="([^"]*)"', front):
        assert not href.startswith("/"), f"root-relative reference {href!r} breaks under file://"


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------


def test_two_builds_are_byte_identical(tmp_path):
    first = tmp_path / "a"
    second = tmp_path / "b"
    build.build_site(REPO_ROOT, first / "site", first / "single.html")
    build.build_site(REPO_ROOT, second / "site", second / "single.html")

    left = {
        path.relative_to(first).as_posix(): path.read_bytes() for path in first.rglob("*") if path.is_file()
    }
    right = {
        path.relative_to(second).as_posix(): path.read_bytes() for path in second.rglob("*") if path.is_file()
    }
    assert sorted(left) == sorted(right)
    differing = [name for name in sorted(left) if left[name] != right[name]]
    assert not differing, f"non-deterministic output: {differing}"


def test_the_generator_stamps_no_time_or_host_into_its_own_chrome(site):
    """Documents may quote timestamps; the generator may not add any.

    The determinism test above compares whole builds, so a clock would already
    fail it. This one names the failure: it looks only at the parts the
    generator writes itself, where a stray "Generated on …" would hide.
    """
    out_dir: Path = site["out_dir"]
    single: str = site["single_html"]
    chrome = [
        (out_dir / "assets" / "docs.js").read_text(encoding="utf-8"),
        (out_dir / "assets" / "docs.css").read_text(encoding="utf-8"),
        single.split("<body", 1)[0],
        single.split('<div class="site-shell">', 1)[0],
    ]
    for fragment in chrome:
        assert not re.search(r"\b(19|20)\d\d-\d\d-\d\d", fragment), "a date reached the generated chrome"
        for phrase in ("Generated on", "Last updated", "Built at ", "Build date"):
            assert phrase not in fragment
    assert "Date.now" not in chrome[0] and "new Date" not in chrome[0]


def test_committed_site_matches_the_documentation():
    """The committed artefact is the deliverable; this is what keeps it honest."""
    if not OUT_DIR.is_dir():
        pytest.skip("the site has not been generated into the repository yet")
    stale = build.check_up_to_date(REPO_ROOT, OUT_DIR, SINGLE_FILE)
    assert not stale, "the committed help site no longer matches docs/ and README.md:\n" + "\n".join(
        f"  - {item}" for item in stale
    )


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_search_index_covers_every_page(site):
    records = build.build_index(site["pages"])
    covered = {record["p"] for record in records}
    assert covered == set(range(len(site["pages"]))), "a page is missing from the search index"
    for position, page in enumerate(site["pages"]):
        leads = [record for record in records if record["p"] == position and record["a"] == ""]
        assert leads, f"{page.rel} has no lead search record"


def test_search_index_reaches_the_headings_of_every_page(site):
    records = build.build_index(site["pages"])
    by_page: dict[int, set[str]] = {}
    for record in records:
        by_page.setdefault(int(record["p"]), set()).add(str(record["a"]))
    for position, page in enumerate(site["pages"]):
        expected = {heading.slug for heading in page.document.headings}
        assert expected <= by_page[position], (
            f"{page.rel}: sections missing from the index: {sorted(expected - by_page[position])}"
        )


def test_search_index_carries_real_text(site):
    records = build.build_index(site["pages"])
    with_text = [record for record in records if str(record["t"]).strip()]
    assert len(with_text) > len(records) // 2, "most indexed sections have no searchable text"
    haystack = " ".join(str(record["t"]) for record in records).lower()
    for word in ("alarm", "registry", "commissioning"):
        assert word in haystack, f"the index does not contain {word!r}"


def test_search_index_is_embedded_in_both_shapes(site):
    single: str = site["single_html"]
    assert "window.__CHAOS_DOCS__" in single
    index_js = site["out_dir"] / "assets" / "search-index.js"
    assert index_js.is_file()
    assert "window.__CHAOS_DOCS__" in index_js.read_text(encoding="utf-8")


def test_index_payload_cannot_break_out_of_its_script_tag(site):
    """Documents are full of angle brackets; none of them may close the script."""
    payload = build.dump_payload(site["pages"], build.build_index(site["pages"]), "single")
    assert "<" not in payload
    assert "://" not in payload


# ---------------------------------------------------------------------------
# design system
# ---------------------------------------------------------------------------


def test_the_site_uses_the_console_design_tokens(site):
    css = (site["out_dir"] / "assets" / "docs.css").read_text(encoding="utf-8")
    tokens = (REPO_ROOT / "src" / "chaos" / "web" / "tokens.css").read_text(encoding="utf-8")
    assert tokens.strip() in css, "the site does not embed the console's tokens verbatim"
    for token in ("--surface-0", "--text-1", "--accent", "--sev-critical", "--status-stale"):
        assert f"var({token})" in css, f"the site does not use {token}"
    assert "@import" not in css, "an @import would be a second request"
    assert "@font-face" not in css, "webfonts are not allowed"


def test_both_themes_can_be_chosen_explicitly(site):
    css = (site["out_dir"] / "assets" / "docs.css").read_text(encoding="utf-8")
    assert ':root[data-theme="dark"]' in css
    assert ':root[data-theme="light"]' in css
    assert "prefers-color-scheme: light" in css, "the system preference must still work"


def test_there_is_a_print_stylesheet(site):
    css = (site["out_dir"] / "assets" / "docs.css").read_text(encoding="utf-8")
    assert "@media print" in css


# ---------------------------------------------------------------------------
# content richness
# ---------------------------------------------------------------------------


def test_code_blocks_are_highlighted_and_copyable(site):
    blob = "".join(path.read_text(encoding="utf-8") for path in _html_files(site["out_dir"]))
    assert "doc-copy" in blob, "code blocks have no copy control"
    assert re.search(r'<span class="t-[a-z]">', blob), "no syntax highlighting reached the output"
    assert 'class="lang-' in blob


def test_headings_carry_anchor_links(site):
    blob = (site["out_dir"] / "index.html").read_text(encoding="utf-8")
    assert 'class="doc-anchor"' in blob


def test_tables_and_lists_render_as_real_elements(site):
    blob = "".join(path.read_text(encoding="utf-8") for path in _html_files(site["out_dir"]))
    assert "<table>" in blob and "<thead>" in blob
    assert "<ul>" in blob and "<ol" in blob
    assert 'class="doc-table-wrap"' in blob


def test_mermaid_blocks_are_shown_as_source_not_dropped(site):
    """No diagram library can be loaded, so the source is shown honestly."""
    mermaid_pages = [
        page
        for page in site["pages"]
        if any(
            block.kind == "code" and (block.data.get("info") or "").strip().lower().startswith("mermaid")
            for block in page.document.blocks
        )
    ]
    if not mermaid_pages:
        pytest.skip("the documentation currently contains no Mermaid diagrams")
    blob = "".join(path.read_text(encoding="utf-8") for path in _html_files(site["out_dir"]))
    assert "doc-diagram" in blob


def test_keyboard_affordances_are_present(site):
    runtime_js = (site["out_dir"] / "assets" / "docs.js").read_text(encoding="utf-8")
    for hint in ('"/"', "ArrowDown", "ArrowUp", "Escape", "Enter"):
        assert hint in runtime_js, f"the runtime does not handle {hint}"
    front = (site["out_dir"] / "index.html").read_text(encoding="utf-8")
    assert 'id="search-input"' in front
    assert 'class="skip-link"' in front


# ---------------------------------------------------------------------------
# the Markdown subset
# ---------------------------------------------------------------------------


def _render(markdown: str) -> str:
    document = build.mdparse.parse(markdown)

    def link(dest: str, label: str):
        return build.mdparse.LinkTarget(href=dest, kind="external")

    ctx = build.mdparse.InlineContext(
        resolve_link=link,
        resolve_image=link,
        link_defs=document.link_defs,
        footnote_defs=document.footnote_defs,
    )
    return build.mdparse.render(document, ctx, build.hl.highlight).html


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        ("# Title", '<h1 id="title"'),
        ("Some *emphasis* here", "<em>emphasis</em>"),
        ("Some **strong** here", "<strong>strong</strong>"),
        ("Some ~~gone~~ here", "<del>gone</del>"),
        ("Use `chaos status`", "<code>chaos status</code>"),
        ("A ``literal ` tick`` span", "literal ` tick"),
        ("- one\n- two", "<ul><li>one</li><li>two</li></ul>"),
        ("3. three\n4. four", '<ol start="3">'),
        ("- [ ] todo\n- [x] done", 'class="doc-task"'),
        ("> quoted", "<blockquote>"),
        ("> [!WARNING]\n> mind this", 'data-tone="warning"'),
        ("| a | b |\n|---:|---|\n| 1 | 2 |", 'class="col-right"'),
        ("| a |\n|---|\n| x \\| y |", "x | y"),
        ("```sh\nls -l\n```", 'class="lang-sh"'),
        ("```mermaid\ngraph TD\n```", "doc-diagram"),
        ("---", "<hr>"),
        ("Setext\n======", '<h1 id="setext"'),
        ("word_with_underscores stays", "word_with_underscores"),
        ("A line  \nbroken", "<br>"),
        ("Escaped \\*not emphasis\\*", "*not emphasis*"),
        ("An &amp; entity", "&amp;"),
        ("Text[^n] here\n\n[^n]: the note", 'class="fn-ref"'),
        ("[ref][r]\n\n[r]: docs/api.md", "ref"),
        ("Hard <br> break", "<br>"),
    ],
)
def test_markdown_subset(markdown, expected):
    assert expected in _render(markdown)


def test_unsupported_raw_html_is_escaped_not_executed():
    rendered = _render('An <span onclick="boom()">injection</span> attempt')
    assert "<span onclick" not in rendered
    assert "&lt;span" in rendered


def _minimal_repo(tmp_path: Path, body: str) -> Path:
    fake = tmp_path / "repo"
    (fake / "docs").mkdir(parents=True)
    (fake / "src" / "chaos" / "web").mkdir(parents=True)
    (fake / "src" / "chaos" / "web" / "tokens.css").write_text(
        ":root { --accent: #4ade80; }\n", encoding="utf-8"
    )
    (fake / "README.md").write_text("# Root\n", encoding="utf-8")
    (fake / "docs" / "page.md").write_text(body, encoding="utf-8")
    return fake


def test_markdown_we_do_not_support_is_reported_and_can_fail_the_build(tmp_path):
    """Unsupported syntax degrades to visible text — and says so."""
    fake = _minimal_repo(tmp_path, "# Page\n\nA <details open>disclosure</details> block.\n")

    report = build.build_site(fake, tmp_path / "out", None)
    assert any("raw inline HTML" in warning for warning in report.warnings), report.warnings
    rendered = (tmp_path / "out" / "page.html").read_text(encoding="utf-8")
    assert "&lt;details" in rendered, "the author's text must still be on the page"

    with pytest.raises(build.BuildError) as error:
        build.build_site(fake, tmp_path / "strict", None, strict=True)
    assert "raw inline HTML" in str(error.value)


def test_a_nested_footnote_definition_is_reported_not_swallowed(tmp_path):
    fake = _minimal_repo(tmp_path, "# Page\n\n- item\n\n    [^x]: a note nested in a list\n")
    report = build.build_site(fake, tmp_path / "out", None)
    assert any("footnote definition" in warning for warning in report.warnings), report.warnings


def test_absolute_urls_are_neutralised_but_still_readable():
    rendered = _render("Open <http://127.0.0.1:8000> now")
    assert "http://" not in rendered
    assert "http&#58;//127.0.0.1:8000" in rendered
    # A browser decodes the reference, so the link still points where it said.
    assert html.unescape(rendered).count("http://127.0.0.1:8000") >= 2


def test_slugs_match_github():
    assert build.mdparse.slugify("1. Black start — recovery") == "1-black-start--recovery"
    assert build.mdparse.slugify("Setup says `needs_attention`") == "setup-says-needs_attention"
    assert build.mdparse.slugify("What now?") == "what-now"


def test_anchor_matching_tiers():
    anchors = {"9-alarms--sdd-14", "6-serviceability--a-dark-tile", "2-errors"}
    assert build.match_anchor("2-errors", anchors) == ("2-errors", None)
    assert build.match_anchor("6-serviceability-a-dark-tile", anchors)[0] == "6-serviceability--a-dark-tile"
    matched, warning = build.match_anchor("9-alarms", anchors)
    assert matched == "9-alarms--sdd-14" and warning
    assert build.match_anchor("nope", anchors) == (None, None)


def test_ambiguous_prefix_is_a_failure_not_a_guess():
    anchors = {"3-control-modes", "3-control-interlocks"}
    assert build.match_anchor("3-control", anchors) == (None, None)


# ---------------------------------------------------------------------------
# structure and robustness
# ---------------------------------------------------------------------------


def test_navigation_lists_every_page_in_reading_order(site):
    front = (site["out_dir"] / "index.html").read_text(encoding="utf-8")
    nav = front.split('<nav class="site-nav"', 1)[1].split("</nav>", 1)[0]
    for page in site["pages"]:
        assert f'data-page="{page.slug}"' in nav, f"{page.rel} is not in the navigation"
    order = re.findall(r'data-page="([^"]+)"', nav)
    assert order == [page.slug for page in site["pages"]], "navigation is not in reading order"


def test_every_page_has_a_table_of_contents_when_it_has_headings(site):
    out_dir: Path = site["out_dir"]
    for page in site["pages"]:
        deep = [heading for heading in page.document.headings if 2 <= heading.level <= 4]
        if not deep:
            continue
        name = "index.html" if page.number == 1 else f"{page.slug}.html"
        text = (out_dir / name).read_text(encoding="utf-8")
        toc = text.split('<aside class="site-toc"', 1)[1].split("</aside>", 1)[0]
        assert "On this page" in toc
        assert f'data-anchor="{deep[0].slug}"' in toc


def test_slugs_are_unique_and_router_safe(site):
    slugs = [page.slug for page in site["pages"]]
    assert len(slugs) == len(set(slugs)), "two pages share a slug"
    for slug in slugs:
        assert re.fullmatch(r"[a-z0-9-]+", slug), f"{slug!r} is not a safe file name"
        assert "__" not in slug, "the single-file router splits page from anchor on __"


def test_output_location_is_a_parameter_not_a_constant(tmp_path):
    elsewhere = tmp_path / "somewhere" / "else"
    other = tmp_path / "one-file.html"
    report = build.build_site(REPO_ROOT, elsewhere, other)
    assert (elsewhere / "index.html").is_file()
    assert other.is_file()
    assert report.pages


def test_rebuilding_the_directory_does_not_delete_the_offline_copy(tmp_path):
    """The offline file sits inside the served folder. It must survive a rebuild."""
    out_dir = tmp_path / "site"
    single = out_dir / "chaos-help-offline.html"
    build.build_site(REPO_ROOT, out_dir, single)
    before = single.read_bytes()

    build.build_site(REPO_ROOT, out_dir, None, preserve=(single,))
    assert single.is_file(), "the directory rebuild deleted the offline copy"
    assert single.read_bytes() == before


def test_a_removed_document_leaves_no_stale_page_behind(tmp_path):
    fake = tmp_path / "repo"
    (fake / "docs").mkdir(parents=True)
    (fake / "src" / "chaos" / "web").mkdir(parents=True)
    (fake / "src" / "chaos" / "web" / "tokens.css").write_text(
        ":root { --accent: #4ade80; }\n", encoding="utf-8"
    )
    (fake / "README.md").write_text("# Root\n", encoding="utf-8")
    (fake / "docs" / "temporary.md").write_text("# Temporary\n", encoding="utf-8")

    out_dir = tmp_path / "out"
    build.build_site(fake, out_dir, None)
    assert (out_dir / "temporary.html").is_file()

    (fake / "docs" / "temporary.md").unlink()
    build.build_site(fake, out_dir, None)
    assert not (out_dir / "temporary.html").exists(), "a deleted document left its page behind"


def test_the_generator_survives_a_restructured_document_set(tmp_path):
    """Documents are being rewritten; renaming or adding one must not break the build."""
    fake = tmp_path / "repo"
    (fake / "docs" / "deep" / "deeper").mkdir(parents=True)
    (fake / "src" / "chaos" / "web").mkdir(parents=True)
    (fake / "src" / "chaos" / "web" / "tokens.css").write_text(
        ":root { --accent: #4ade80; }\n@media (prefers-color-scheme: light) { :root { --accent: #096240; } }\n",
        encoding="utf-8",
    )
    (fake / "README.md").write_text("# Root\n\n[deep](docs/deep/deeper/new.md)\n", encoding="utf-8")
    (fake / "docs" / "index.md").write_text(
        "# Index\n\n## The set\n\n### Group\n\n| D |\n|---|\n| [New](./deep/deeper/new.md) |\n",
        encoding="utf-8",
    )
    (fake / "docs" / "deep" / "deeper" / "new.md").write_text(
        "# Brand new\n\n## A section\n\ntext\n", encoding="utf-8"
    )
    report = build.build_site(fake, tmp_path / "out", tmp_path / "one.html")
    assert {page.rel for page in report.pages} == {
        "README.md",
        "docs/index.md",
        "docs/deep/deeper/new.md",
    }
    assert (tmp_path / "out" / "deep-deeper-new.html").is_file()


def test_an_empty_source_set_is_an_error_not_an_empty_site(tmp_path):
    fake = tmp_path / "repo"
    (fake / "src" / "chaos" / "web").mkdir(parents=True)
    (fake / "src" / "chaos" / "web" / "tokens.css").write_text(":root{}", encoding="utf-8")
    with pytest.raises(build.BuildError):
        build.build_site(fake, tmp_path / "out", None)
