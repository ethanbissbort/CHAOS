"""Computed WCAG contrast and signal-redundancy checks for the console palette.

This is the CHAOS answer to Rackula's ``colour-presets-contrast.test.ts``: the
palette is not asserted by eye or frozen as a list of hex literals someone
eyeballed once. Every pairing the console actually renders is parsed out of
``tokens.css``, composited the way the browser composites it, and measured.

Three families of assertion live here.

1. **WCAG 2.1 AA, computed.** 4.5:1 for body text, 3:1 for large text and for
   non-text indicators (status dots, meter fills, focus rings, category
   swatches). Both themes. The thresholds are the standard's; if a colour you
   want fails, change the colour.

2. **The two load-bearing distinctions.** ``stale`` must not look like ``ok``
   and ``no_data`` must not look like a healthy zero. Measured as CIE Lab
   separation, and again after simulating protanopia, deuteranopia and
   tritanopia -- an operator who cannot tell green from amber is exactly the
   operator who must not mistake a dead sensor for a good reading.

3. **Colour is never the only channel.** Verified against the real artefacts:
   the glyph and word come from ``app.js``, the border style and fill come from
   ``styles.css``. For every pair of availability states at least two
   non-colour channels differ, so the distinction survives a monochrome
   photograph of the wall display.
"""

from __future__ import annotations

import itertools
import math
import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "src" / "chaos" / "web"
TOKENS_CSS = WEB / "tokens.css"
STYLES_CSS = WEB / "styles.css"
APP_JS = WEB / "app.js"

AA_TEXT = 4.5
AA_LARGE = 3.0
AA_NON_TEXT = 3.0

# The six availability words the API speaks, in ``overview.py`` order.
AVAILABILITY = (
    "ok",
    "stale",
    "no_data",
    "no_points",
    "design_only",
    "not_deployed",
)
SEVERITIES = ("emergency", "critical", "major", "warning", "info")


# ---------------------------------------------------------------------------
# Colour maths
# ---------------------------------------------------------------------------


def srgb(value: str) -> tuple[float, float, float, float]:
    """Parse a CSS colour literal into 0..1 RGBA. Only the forms tokens.css uses."""
    text = value.strip()
    match = re.fullmatch(r"#([0-9a-fA-F]{3,8})", text)
    if match:
        digits = match.group(1)
        if len(digits) in (3, 4):
            digits = "".join(c * 2 for c in digits)
        parts = [int(digits[i : i + 2], 16) / 255 for i in range(0, len(digits), 2)]
        if len(parts) == 3:
            parts.append(1.0)
        return tuple(parts)  # type: ignore[return-value]
    match = re.fullmatch(r"rgba?\(([^)]*)\)", text)
    if match:
        raw = [p.strip() for p in re.split(r"[,\s/]+", match.group(1)) if p.strip()]
        nums = [float(p[:-1]) / 100 if p.endswith("%") else float(p) for p in raw]
        rgb = [n / 255 for n in nums[:3]]
        alpha = nums[3] if len(nums) > 3 else 1.0
        return (rgb[0], rgb[1], rgb[2], alpha)
    raise AssertionError(f"tokens.css contains a colour this test cannot parse: {value!r}")


def _linear(channel: float) -> float:
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def luminance(colour: str) -> float:
    r, g, b, _ = srgb(colour)
    return 0.2126 * _linear(r) + 0.7152 * _linear(g) + 0.0722 * _linear(b)


def contrast(fg: str, bg: str) -> float:
    """WCAG 2.1 contrast ratio. Both arguments must already be opaque."""
    a, b = luminance(fg), luminance(bg)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def composite(fg: str, bg: str, alpha: float | None = None) -> str:
    """Flatten ``fg`` (optionally at ``alpha``) over opaque ``bg``, as the browser does."""
    fr, fg_, fb, fa = srgb(fg)
    br, bg_, bb, _ = srgb(bg)
    a = fa if alpha is None else alpha
    out = (fr * a + br * (1 - a), fg_ * a + bg_ * (1 - a), fb * a + bb * (1 - a))
    return "#" + "".join(f"{round(c * 255):02x}" for c in out)


def lab(colour: str) -> tuple[float, float, float]:
    r, g, b, _ = srgb(colour)
    r, g, b = _linear(r), _linear(g), _linear(b)
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    fx, fy, fz = f(x), f(y), f(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def delta_e(first: str, second: str) -> float:
    """CIE76 colour difference. Blunt, but adequate for 'are these obviously different'."""
    return math.dist(lab(first), lab(second))


#: Machado, Oliveira & Fernandes (2009) severity-1.0 matrices, applied in linear RGB.
CVD_MATRICES = {
    "protanopia": (
        (0.152286, 1.052583, -0.204868),
        (0.114503, 0.786281, 0.099216),
        (-0.003882, -0.048116, 1.051998),
    ),
    "deuteranopia": (
        (0.367322, 0.860646, -0.227968),
        (0.280085, 0.672501, 0.047413),
        (-0.011820, 0.042940, 0.968881),
    ),
    "tritanopia": (
        (1.255528, -0.076749, -0.178779),
        (-0.078411, 0.930809, 0.147602),
        (0.004733, 0.691367, 0.303900),
    ),
}


def simulate_cvd(colour: str, kind: str) -> str:
    def unlinear(channel: float) -> float:
        channel = min(1.0, max(0.0, channel))
        return channel * 12.92 if channel <= 0.0031308 else 1.055 * channel ** (1 / 2.4) - 0.055

    r, g, b, _ = srgb(colour)
    r, g, b = _linear(r), _linear(g), _linear(b)
    matrix = CVD_MATRICES[kind]
    out = [matrix[i][0] * r + matrix[i][1] * g + matrix[i][2] * b for i in range(3)]
    return "#" + "".join(f"{round(unlinear(c) * 255):02x}" for c in out)


# ---------------------------------------------------------------------------
# tokens.css parsing
# ---------------------------------------------------------------------------

_DECL = re.compile(r"(--[\w-]+)\s*:\s*([^;{}]+);")


def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", " ", css, flags=re.DOTALL)


def _block(css: str, start: int) -> str:
    """Return the text of the brace-balanced block whose opening ``{`` is at ``start``."""
    depth = 0
    for index in range(start, len(css)):
        if css[index] == "{":
            depth += 1
        elif css[index] == "}":
            depth -= 1
            if depth == 0:
                return css[start + 1 : index]
    raise AssertionError("unbalanced braces in tokens.css")


def _declarations(block: str) -> dict[str, str]:
    return {name: value.strip() for name, value in _DECL.findall(block)}


def load_themes() -> dict[str, dict[str, str]]:
    """Resolve tokens.css into a flat {token: literal} map per theme.

    ``var(--x)`` aliases are followed transitively, which is how the browser
    resolves them, so the compatibility aliases (``--st-ok`` and friends) are
    measured as the values they actually paint.
    """
    css = _strip_comments(TOKENS_CSS.read_text(encoding="utf-8"))

    root_start = css.index("{", css.index(":root"))
    dark = _declarations(_block(css, root_start))

    light_at = css.index("prefers-color-scheme: light")
    light_root = css.index(":root", light_at)
    light = dict(dark)
    light.update(_declarations(_block(css, css.index("{", light_root))))

    def resolve(theme: dict[str, str]) -> dict[str, str]:
        out: dict[str, str] = {}

        def value_of(name: str, seen: frozenset[str] = frozenset()) -> str:
            assert name not in seen, f"{name} is part of a var() cycle"
            raw = theme[name]
            match = re.fullmatch(r"var\((--[\w-]+)\)", raw.strip())
            if match:
                return value_of(match.group(1), seen | {name})
            return raw

        for name in theme:
            out[name] = value_of(name)
        return out

    return {"dark": resolve(dark), "light": resolve(light)}


THEMES = load_themes()
DARK = THEMES["dark"]
LIGHT = THEMES["light"]
TOKENS_TEXT = TOKENS_CSS.read_text(encoding="utf-8")
STYLES_TEXT = STYLES_CSS.read_text(encoding="utf-8")
APP_TEXT = APP_JS.read_text(encoding="utf-8")

#: Every surface a chip, a status colour or a piece of text is painted onto.
#: ``--surface-3`` is a pressed state only and carries no text, so it is not here.
TEXT_SURFACES = ("--surface-0", "--surface-1", "--surface-2", "--surface-sunken", "--surface-input")

CHIP_TINT = float(DARK["--chip-tint"].rstrip("%")) / 100


def report(label: str, ratio: float, floor: float) -> str:
    verdict = "PASS" if ratio >= floor else "FAIL"
    return f"{verdict} {label}: {ratio:.2f}:1 (needs {floor}:1)"


def ratios(theme: str) -> dict[str, str]:
    return THEMES[theme]


# ---------------------------------------------------------------------------
# 1. WCAG AA
# ---------------------------------------------------------------------------

THEME_IDS = ("dark", "light")


@pytest.mark.parametrize("theme", THEME_IDS)
@pytest.mark.parametrize(
    "token",
    ["--text-1", "--text-2", "--text-3"],
)
@pytest.mark.parametrize("surface", TEXT_SURFACES)
def test_text_ranks_clear_aa_on_every_surface(theme: str, token: str, surface: str) -> None:
    """All three text ranks are body text. ``--text-3`` carries provenance and
    notes -- the sentences that say whether a number can be trusted -- so it is
    held to 4.5:1 like the rest, not to the 3:1 large-text allowance."""
    palette = ratios(theme)
    got = contrast(palette[token], palette[surface])
    assert got >= AA_TEXT, report(f"{theme} {token} on {surface}", got, AA_TEXT)


@pytest.mark.parametrize("theme", THEME_IDS)
@pytest.mark.parametrize("surface", ("--surface-0", "--surface-1", "--surface-sunken"))
def test_accent_text_clears_aa(theme: str, surface: str) -> None:
    """``--accent`` is text: `.linkish` buttons and the sidenav's active label."""
    palette = ratios(theme)
    got = contrast(palette["--accent"], palette[surface])
    assert got >= AA_TEXT, report(f"{theme} --accent on {surface}", got, AA_TEXT)


@pytest.mark.parametrize("theme", THEME_IDS)
def test_ink_on_accent_fill_clears_aa(theme: str) -> None:
    """`.btn-primary` and `.skip-link` put --accent-ink on a solid --accent."""
    palette = ratios(theme)
    for ink in ("--accent-ink", "--text-on-accent"):
        got = contrast(palette[ink], palette["--accent"])
        assert got >= AA_TEXT, report(f"{theme} {ink} on --accent", got, AA_TEXT)
    strong = contrast(palette["--accent-ink"], palette["--accent-strong"])
    assert strong >= AA_TEXT, report(f"{theme} --accent-ink on --accent-strong", strong, AA_TEXT)


def _status_tokens() -> list[str]:
    return [f"--status-{name.replace('_', '-')}" for name in AVAILABILITY] + [
        "--status-alarm",
        "--status-degraded",
        "--status-unknown",
    ]


def _severity_tokens() -> list[str]:
    return [f"--sev-{name}" for name in SEVERITIES]


@pytest.mark.parametrize("theme", THEME_IDS)
@pytest.mark.parametrize("token", _status_tokens() + _severity_tokens())
@pytest.mark.parametrize("surface", TEXT_SURFACES)
def test_chip_text_clears_aa_over_its_own_tint(theme: str, token: str, surface: str) -> None:
    """`.chip` paints its label in the token colour over a --chip-tint wash of
    the same colour. The wash pulls the background toward the text, so the
    honest measurement is against the composited result, not the bare surface."""
    palette = ratios(theme)
    colour = palette[token]
    background = composite(colour, palette[surface], CHIP_TINT)
    got = contrast(colour, background)
    assert got >= AA_TEXT, report(f"{theme} {token} chip on {surface}", got, AA_TEXT)


@pytest.mark.parametrize("theme", THEME_IDS)
@pytest.mark.parametrize("token", _status_tokens() + _severity_tokens())
@pytest.mark.parametrize("surface", TEXT_SURFACES)
def test_status_colour_as_plain_text_clears_aa(theme: str, token: str, surface: str) -> None:
    """Views also set these colours as bare text (`views/control.js` writes
    ``color:var(--sev-major)`` on a card, `views/energy.js` on the SVG chart)."""
    palette = ratios(theme)
    got = contrast(palette[token], palette[surface])
    assert got >= AA_TEXT, report(f"{theme} {token} text on {surface}", got, AA_TEXT)


@pytest.mark.parametrize("theme", THEME_IDS)
@pytest.mark.parametrize("severity", SEVERITIES)
def test_ink_on_solid_severity_fill_clears_aa(theme: str, severity: str) -> None:
    """`.chip-solid` and `.nav-badge` sit on a filled severity colour. The badge
    is the count of active alarms in the primary nav, which makes it the single
    most consequential number on the screen."""
    palette = ratios(theme)
    got = contrast(palette["--sev-ink"], palette[f"--sev-{severity}"])
    assert got >= AA_TEXT, report(f"{theme} --sev-ink on --sev-{severity}", got, AA_TEXT)


@pytest.mark.parametrize("theme", THEME_IDS)
@pytest.mark.parametrize("token", _status_tokens() + _severity_tokens() + ["--accent", "--focus"])
@pytest.mark.parametrize("surface", ("--surface-0", "--surface-1", "--surface-sunken"))
def test_indicators_clear_non_text_contrast(theme: str, token: str, surface: str) -> None:
    """Freshness dots, meter fills, map markers, card keylines, the focus ring
    and the legend swatches carry meaning without carrying text."""
    palette = ratios(theme)
    got = contrast(palette[token], palette[surface])
    assert got >= AA_NON_TEXT, report(f"{theme} {token} indicator on {surface}", got, AA_NON_TEXT)


@pytest.mark.parametrize("theme", THEME_IDS)
def test_danger_button_hover_inversion_clears_aa(theme: str) -> None:
    """`.btn-danger:hover` fills with --sev-critical and inks with --sev-ink."""
    palette = ratios(theme)
    got = contrast(palette["--sev-ink"], palette["--sev-critical"])
    assert got >= AA_TEXT, report(f"{theme} danger hover", got, AA_TEXT)


@pytest.mark.parametrize("theme", THEME_IDS)
def test_error_and_offline_washes_keep_their_headings_readable(theme: str) -> None:
    """`.error-box` washes --sev-critical at 10% into the card and `.banner-offline`
    at 18% into it, then writes the heading in --sev-critical on top."""
    palette = ratios(theme)
    for name, alpha in (("error-box", 0.10), ("banner-offline", 0.18)):
        background = composite(palette["--sev-critical"], palette["--surface-1"], alpha)
        got = contrast(palette["--sev-critical"], background)
        assert got >= AA_TEXT, report(f"{theme} {name} heading", got, AA_TEXT)
        body = contrast(palette["--text-1"], background)
        assert body >= AA_TEXT, report(f"{theme} {name} body text", body, AA_TEXT)


@pytest.mark.parametrize("theme", THEME_IDS)
def test_table_row_hover_does_not_break_text(theme: str) -> None:
    palette = ratios(theme)
    for token in ("--text-1", "--text-2", "--text-3"):
        got = contrast(palette[token], palette["--surface-2"])
        assert got >= AA_TEXT, report(f"{theme} {token} on hovered row", got, AA_TEXT)


# ---------------------------------------------------------------------------
# Category scale (Rackula's constraint)
# ---------------------------------------------------------------------------


def category_tokens() -> dict[str, str]:
    """Every ``--cat-*`` colour, excluding the label and the tint percentage."""
    return {
        name: value
        for name, value in DARK.items()
        if name.startswith("--cat-") and name not in ("--cat-label", "--cat-tint")
    }


CATEGORY_FAMILIES = (
    "--cat-compute",
    "--cat-storage",
    "--cat-network",
    "--cat-routing",
    "--cat-power",
    "--cat-generation",
    "--cat-battery",
    "--cat-water",
    "--cat-sensor",
    "--cat-security",
    "--cat-agriculture",
    "--cat-structure",
    "--cat-load",
    "--cat-fallback",
)


@pytest.mark.parametrize("token", sorted(category_tokens()))
def test_category_fill_carries_the_device_label(token: str) -> None:
    """The rack elevation and the topology canvas draw a device name in
    --cat-label on top of the category fill. This is the Rackula assertion: the
    bright accents are excluded from this scale precisely because they fail it."""
    got = contrast(DARK["--cat-label"], category_tokens()[token])
    assert got >= AA_TEXT, report(f"--cat-label on {token}", got, AA_TEXT)


@pytest.mark.parametrize("theme", THEME_IDS)
@pytest.mark.parametrize("token", sorted(category_tokens()))
@pytest.mark.parametrize("surface", ("--surface-0", "--surface-1"))
def test_category_fill_reads_as_a_stroke_in_both_themes(theme: str, token: str, surface: str) -> None:
    """Category colours do not change with the theme, so the same value has to
    stand off both a near-black and a near-white panel as a border or swatch."""
    got = contrast(category_tokens()[token], ratios(theme)[surface])
    assert got >= AA_NON_TEXT, report(f"{theme} {token} stroke on {surface}", got, AA_NON_TEXT)


@pytest.mark.parametrize("pair", list(itertools.combinations(CATEGORY_FAMILIES, 2)), ids=str)
def test_category_families_are_visibly_distinct(pair: tuple[str, str]) -> None:
    """Two devices from different families must never read as the same colour."""
    first, second = (DARK[name] for name in pair)
    got = delta_e(first, second)
    assert got >= 15.0, f"{pair[0]} and {pair[1]} are only {got:.1f} dE76 apart"


def test_every_asset_class_in_the_dictionary_has_a_category_token() -> None:
    """tokens.css claims to cover the whole asset-class dictionary. Check it."""
    import json

    dictionary = Path(__file__).resolve().parents[1] / "data" / "asset_class_dictionary.json"
    if not dictionary.exists():  # pragma: no cover - design package is optional at runtime
        pytest.skip("design package not present")
    classes = json.loads(dictionary.read_text(encoding="utf-8"))["asset_classes"]
    missing = [name for name in classes if f"--cat-{name.replace('_', '-')}" not in DARK]
    assert not missing, f"asset classes with no --cat-* token: {sorted(missing)}"


def test_every_rack_legend_bucket_has_a_category_token() -> None:
    """The rack elevation paints straight from the payload:
    ``var(--cat-bucket-${device.category})``. Every bucket the API can return
    has to resolve, or a device silently loses its fill."""
    rack = pytest.importorskip("chaos.api.routers.rack")
    buckets = set(rack.CATEGORY_LABELS) | set(rack.CATEGORIES.values()) | {rack.DEFAULT_CATEGORY}
    missing = [name for name in sorted(buckets) if f"--cat-bucket-{name}" not in DARK]
    assert not missing, f"rack legend buckets with no --cat-bucket-* token: {missing}"


def test_every_sdd_domain_has_a_category_token() -> None:
    """Same promise for anything colouring by subsystem instead of by class."""
    overview = pytest.importorskip("chaos.api.routers.overview")
    missing = [name for name, _ in overview.DOMAINS if f"--cat-domain-{name}" not in DARK]
    assert not missing, f"SDD 25.4 domains with no --cat-domain-* token: {missing}"


# ---------------------------------------------------------------------------
# 2. The load-bearing distinctions
# ---------------------------------------------------------------------------

#: Below this two colours start to look like the same colour with a different
#: rendering. 15 dE76 is roughly "obviously a different colour" at chip size.
DISTINCT = 15.0
#: Under a simulated colour-vision deficiency the same pair may collapse toward
#: each other; 9 dE76 keeps a residual lightness/chroma difference. Anything
#: relying on less than that must carry the meaning in glyph and word too, which
#: test_colour_is_never_the_only_channel enforces for every pair regardless.
DISTINCT_CVD = 9.0


@pytest.mark.parametrize("theme", THEME_IDS)
@pytest.mark.parametrize("other", ("stale", "no_data", "no_points", "design_only", "not_deployed"))
def test_only_ok_looks_like_ok(theme: str, other: str) -> None:
    """The whole console rests on this. ``stale`` means the instrument stopped
    reporting and the last number is a fossil; ``no_data`` means nothing has
    ever arrived. Neither is a healthy reading, and neither may borrow the
    colour of one."""
    palette = ratios(theme)
    ok = palette["--status-ok"]
    candidate = palette[f"--status-{other.replace('_', '-')}"]
    got = delta_e(ok, candidate)
    assert got >= 25.0, f"{theme}: --status-{other} is only {got:.1f} dE76 from --status-ok"
    for kind in CVD_MATRICES:
        simulated = delta_e(simulate_cvd(ok, kind), simulate_cvd(candidate, kind))
        assert simulated >= DISTINCT_CVD, (
            f"{theme}: under {kind}, --status-{other} collapses to {simulated:.1f} dE76 from --status-ok"
        )


def test_ok_is_the_only_bright_availability_colour_on_the_wall_display() -> None:
    """Dark is the console's default and the wall display never leaves it. There,
    ``ok`` is the brightest availability colour by a clear margin: everything the
    platform does not actually know is dimmer, so an operator reading the room
    from six metres away sees knowledge, not decoration."""
    ok = luminance(DARK["--status-ok"])
    for name in AVAILABILITY[1:]:
        other = luminance(DARK[f"--status-{name.replace('_', '-')}"])
        ratio = (ok + 0.05) / (other + 0.05)
        assert ratio >= 1.4, f"--status-{name} is within {ratio:.2f}x of --status-ok in luminance"


@pytest.mark.parametrize("theme", THEME_IDS)
def test_stale_is_not_the_warning_colour(theme: str) -> None:
    """``stale`` and ``warning`` used to be the same hex. They mean opposite
    things: one says the platform has stopped knowing, the other says it knows
    and does not like the answer."""
    palette = ratios(theme)
    got = delta_e(palette["--status-stale"], palette["--sev-warning"])
    assert got >= 15.0, f"{theme}: --status-stale is only {got:.1f} dE76 from --sev-warning"


@pytest.mark.parametrize("theme", THEME_IDS)
def test_no_data_is_never_green(theme: str) -> None:
    """A cool hue for absence, so a glance never reads "no_data" as "fine"."""
    palette = ratios(theme)
    for name in ("no-data", "no-points"):
        _, _, blue_minus_yellow = lab(palette[f"--status-{name}"])
        assert blue_minus_yellow < 0, f"{theme}: --status-{name} sits on the warm side of Lab b*"


# ---------------------------------------------------------------------------
# 3. Colour is never the only channel -- checked against the real markup
# ---------------------------------------------------------------------------


def parse_status_meta() -> dict[str, dict[str, str]]:
    """Pull STATUS_META out of app.js. That object is what actually renders."""
    block = re.search(r"export const STATUS_META\s*=\s*\{(.*?)\n\};", APP_TEXT, re.DOTALL)
    assert block, "app.js no longer exports STATUS_META in the expected shape"
    entries = re.findall(r"(\w+)\s*:\s*\{\s*glyph:\s*'([^']*)'\s*,\s*label:\s*'([^']*)'", block.group(1))
    return {name: {"glyph": glyph, "label": label} for name, glyph, label in entries}


def parse_severity_meta() -> dict[str, dict[str, str]]:
    block = re.search(r"export const SEVERITY_META\s*=\s*\{(.*?)\n\};", APP_TEXT, re.DOTALL)
    assert block, "app.js no longer exports SEVERITY_META in the expected shape"
    entries = re.findall(r"(\w+)\s*:\s*\{\s*glyph:\s*'([^']*)'\s*,\s*label:\s*'([^']*)'", block.group(1))
    return {name: {"glyph": glyph, "label": label} for name, glyph, label in entries}


def chip_rule(status: str) -> str:
    """The single ``.chip-<status>`` declaration block from styles.css."""
    match = re.search(rf"\.chip-{re.escape(status)}\s*\{{([^}}]*)\}}", STYLES_TEXT)
    assert match, f"styles.css has no .chip-{status} rule"
    return match.group(1)


def chip_channels(status: str) -> tuple[str, str]:
    """(border-style, fill) as styles.css declares them for one status chip."""
    rule = chip_rule(status)
    border = re.search(r"--chip-border:\s*([\w-]+)", rule)
    fill = "hollow" if re.search(r"background:\s*transparent", rule) else "tinted"
    return (border.group(1) if border else "solid", fill)


def test_every_availability_state_has_a_glyph_and_a_word() -> None:
    meta = parse_status_meta()
    for name in AVAILABILITY:
        assert name in meta, f"app.js STATUS_META is missing {name}"
        assert meta[name]["glyph"].strip(), f"{name} has no glyph"
        assert meta[name]["label"].strip(), f"{name} has no word"


def test_every_severity_has_a_glyph_and_a_word() -> None:
    meta = parse_severity_meta()
    for name in SEVERITIES:
        assert name in meta, f"app.js SEVERITY_META is missing {name}"
        assert meta[name]["glyph"].strip(), f"{name} has no glyph"
        assert meta[name]["label"].strip(), f"{name} has no word"
    glyphs = [meta[name]["glyph"] for name in SEVERITIES]
    assert len(set(glyphs)) == len(glyphs), f"severity glyphs are not unique: {glyphs}"


def test_every_availability_state_has_a_unique_border_and_fill_pair() -> None:
    """Six states, six distinct (border-style, fill) combinations. This is the
    channel that survives a monochrome photo of the wall display."""
    pairs = {name: chip_channels(name) for name in AVAILABILITY}
    assert len(set(pairs.values())) == len(AVAILABILITY), f"chip treatments collide: {pairs}"


@pytest.mark.parametrize("pair", list(itertools.combinations(AVAILABILITY, 2)), ids=str)
def test_colour_is_never_the_only_channel(pair: tuple[str, str]) -> None:
    """For any two availability states, at least two non-colour channels differ.

    Two rather than one: a word alone is only legible up close, and this console
    is read from across a container.
    """
    meta = parse_status_meta()
    first, second = pair
    differing = [
        channel
        for channel, left, right in (
            ("glyph", meta[first]["glyph"], meta[second]["glyph"]),
            ("word", meta[first]["label"], meta[second]["label"]),
            ("border", chip_channels(first)[0], chip_channels(second)[0]),
            ("fill", chip_channels(first)[1], chip_channels(second)[1]),
        )
        if left != right
    ]
    assert len(differing) >= 2, f"{first} vs {second} differ only by {differing} plus colour"


def test_an_unavailable_readout_cannot_be_skim_read_as_a_number() -> None:
    """The big number and the "No data" placeholder must not share a treatment."""
    rule = re.search(r"\.readout\.unavailable \.value\s*\{([^}]*)\}", STYLES_TEXT)
    assert rule, "styles.css no longer distinguishes an unavailable readout"
    body = rule.group(1)
    assert "font-size" in body and "font-weight" in body, "size and weight must both change"
    assert "dotted" in body, "an unavailable readout needs a non-colour marker"


def test_an_unavailable_meter_is_hatched_not_empty() -> None:
    assert "repeating-linear-gradient" in re.search(
        r"\.meter-hatched > span\s*\{([^}]*)\}", STYLES_TEXT
    ).group(1), "an unavailable meter must be hatched, never an empty bar"


# ---------------------------------------------------------------------------
# Structural guarantees for the token layer itself
# ---------------------------------------------------------------------------


def test_styles_consumes_tokens_and_hardcodes_almost_nothing() -> None:
    """styles.css must not reintroduce literal colours. The four permitted
    literals are the annunciator launcher's miniature panel, which deliberately
    echoes annunciator.css rather than the console theme."""
    body = _strip_comments(STYLES_TEXT)
    literals = set(re.findall(r"#[0-9a-fA-F]{3,8}\b", body))
    allowed = {"#11161a", "#2b3a33", "#ff4b32", "#ffa023", "#1a0d05"}
    assert literals <= allowed, (
        f"styles.css hardcodes colours instead of tokens: {sorted(literals - allowed)}"
    )


def test_styles_imports_the_token_layer_first() -> None:
    """Only comments may precede @import. A rule before it and the browser drops
    the import silently, which means an unstyled console rather than an error."""
    body = re.sub(r"^(?:\s*/\*.*?\*/)*\s*", "", STYLES_TEXT, flags=re.DOTALL)
    assert body.startswith('@import url("tokens.css");'), (
        "@import must be the first rule in styles.css or the browser drops it"
    )


def test_styles_never_references_a_token_that_does_not_exist() -> None:
    """A ``var(--typo)`` fails silently: the property becomes invalid at
    computed-value time and the element inherits, which on a dark console shows
    up as an invisible border rather than as an error."""
    body = _strip_comments(STYLES_TEXT)
    used = set(re.findall(r"var\(\s*(--[\w-]+)", body))
    #: Component-local switches, declared and consumed inside styles.css.
    local = set(re.findall(r"(--[\w-]+)\s*:", body))
    unknown = sorted(name for name in used - local if name not in DARK)
    assert not unknown, f"styles.css uses tokens tokens.css does not define: {unknown}"


def test_no_web_asset_reaches_outside_this_server() -> None:
    """SDD 5.1, restated for the files this agent owns: the console must render
    with the uplink down. Attribution belongs in ACKNOWLEDGEMENTS.md, not in a
    comment carrying a URL."""
    for path in (TOKENS_CSS, STYLES_CSS):
        text = path.read_text(encoding="utf-8")
        for needle in ("http://", "https://", "//cdn.", "@font-face", "fonts.googleapis"):
            assert needle not in text, f"{path.name} references {needle}"


def test_compatibility_aliases_still_resolve() -> None:
    """app.js, api.js and views/*.js reference these names in inline styles and
    SVG attributes. Those files belong to other agents; the names must not move."""
    legacy = [
        "--bg",
        "--bg-raised",
        "--bg-sunken",
        "--bg-input",
        "--line",
        "--line-strong",
        "--text",
        "--text-dim",
        "--text-faint",
        "--accent",
        "--accent-ink",
        "--focus",
        "--radius",
        "--radius-s",
        "--gap",
        "--shadow",
        "--fs-base",
        "--st-ok",
        "--st-stale",
        "--st-nodata",
        "--st-designonly",
        "--st-notdeployed",
        "--st-alarm",
        "--st-degraded",
        *(f"--sev-{name}" for name in SEVERITIES),
    ]
    for theme in THEME_IDS:
        missing = [name for name in legacy if name not in ratios(theme)]
        assert not missing, f"{theme} theme dropped tokens other views depend on: {missing}"


def test_every_theme_defines_the_same_token_names() -> None:
    """A token defined only in the dark block silently falls back in light mode."""
    assert set(DARK) == set(LIGHT)


def test_soft_tints_match_the_colour_they_claim_to_tint() -> None:
    """A ``-soft`` token is the base colour at low alpha. If someone edits the
    base and forgets the tint, the wash drifts off-hue without anything failing."""
    for theme in THEME_IDS:
        palette = ratios(theme)
        for name, value in palette.items():
            if not name.endswith("-soft"):
                continue
            base = name[: -len("-soft")]
            if base not in palette:
                continue
            soft_rgb = srgb(value)[:3]
            base_rgb = srgb(palette[base])[:3]
            drift = max(abs(a - b) for a, b in zip(soft_rgb, base_rgb, strict=True))
            assert drift <= 1 / 255 + 1e-9, f"{theme} {name} is not {base} at low alpha"
            assert srgb(value)[3] < 0.5, f"{theme} {name} is not a tint"


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def test_report_achieved_ratios(capsys: pytest.CaptureFixture[str]) -> None:
    """Not an assertion so much as the receipt: run with -s to read the numbers."""
    lines: list[str] = []
    for theme in THEME_IDS:
        palette = ratios(theme)
        lines.append(f"\n=== {theme} ===")
        for token in ("--text-1", "--text-2", "--text-3", "--accent"):
            values = [contrast(palette[token], palette[s]) for s in TEXT_SURFACES]
            lines.append(f"  {token:<10} on surfaces: {min(values):.2f}–{max(values):.2f}:1")
        for token in _status_tokens() + _severity_tokens():
            chip = min(
                contrast(palette[token], composite(palette[token], palette[s], CHIP_TINT))
                for s in TEXT_SURFACES
            )
            lines.append(f"  {token:<24} chip label: {chip:.2f}:1")
        lines.append(
            f"  --sev-ink on solid fills: "
            f"{min(contrast(palette['--sev-ink'], palette[f'--sev-{s}']) for s in SEVERITIES):.2f}:1"
        )
    worst_label = min(contrast(DARK["--cat-label"], v) for v in category_tokens().values())
    lines.append(f"\n  --cat-label on every category fill: >= {worst_label:.2f}:1")
    with capsys.disabled():
        print("\n".join(lines))
    assert lines
