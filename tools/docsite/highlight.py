"""Build-time syntax highlighting.

Highlighting happens in Python, once, at build time. The generated page ships
coloured ``<span>``s and no highlighter — no CDN, no bundled library, no
work for the browser, and nothing to go wrong when the page is opened from a
USB stick during an outage.

Each language is one ordered alternation of named groups. First match wins, so
the ordering inside a spec is the grammar: strings before comments (a ``#``
inside a shell string is not a comment), comments before operators, keywords
before bare words. Unknown languages fall through to plain escaped text — a
missing colour is not worth a wrong one.
"""

from __future__ import annotations

import re

from .mdparse import esc_text

# Token classes, and what they mean visually (see theme.py):
#   c comment   s string   n number   k keyword   t type/builtin
#   p property  v variable f function o operator  d deleted  a added
_CLASS_NAMES = ("c", "s", "n", "k", "t", "p", "v", "f", "o", "d", "a")

ALIASES = {
    "bash": "sh",
    "console": "sh",
    "shell": "sh",
    "shell-session": "sh",
    "zsh": "sh",
    "ksh": "sh",
    "make": "sh",
    "makefile": "sh",
    "dockerfile": "sh",
    "docker": "sh",
    "py": "python",
    "python3": "python",
    "ps1": "powershell",
    "pwsh": "powershell",
    "posh": "powershell",
    "yml": "yaml",
    "jsonc": "json",
    "js": "javascript",
    "ts": "javascript",
    "typescript": "javascript",
    "cs": "csharp",
    "c#": "csharp",
    "htm": "xml",
    "html": "xml",
    "svg": "xml",
    "cfg": "ini",
    "conf": "ini",
    "toml": "ini",
    "env": "ini",
    "md": "markdown",
    "postgres": "sql",
    "psql": "sql",
    "plaintext": "",
    "text": "",
    "txt": "",
    "none": "",
    "output": "",
}


def _kw(words: str) -> str:
    return r"\b(?:" + "|".join(sorted(words.split(), key=len, reverse=True)) + r")\b"


_SH_KEYWORDS = (
    "if then else elif fi for while until do done case esac in function return "
    "export local readonly set unset shift source exit trap eval exec cd echo"
)
_PY_KEYWORDS = (
    "False None True and as assert async await break class continue def del elif "
    "else except finally for from global if import in is lambda nonlocal not or "
    "pass raise return try while with yield match case"
)
_PY_BUILTINS = (
    "abs all any bool bytes dict enumerate float format frozenset getattr hasattr "
    "int isinstance len list map max min object open print range repr reversed set "
    "setattr sorted str sum super tuple type zip self cls"
)
_PS_KEYWORDS = (
    "if else elseif switch foreach for while do until break continue return function "
    "param begin process end try catch finally throw filter in trap exit"
)
_SQL_KEYWORDS = (
    "select insert update delete from where group by order having limit offset join "
    "left right inner outer on as and or not null is in values set into create table "
    "alter drop index view primary key foreign references default distinct union all "
    "with returning case when then else end"
)
_CS_KEYWORDS = (
    "abstract as async await base bool break byte case catch char class const continue "
    "decimal default delegate do double else enum event explicit extern false finally "
    "fixed float for foreach get goto if implicit in int interface internal is lock long "
    "namespace new null object operator out override params private protected public "
    "readonly record ref return sbyte sealed set short sizeof stackalloc static string "
    "struct switch this throw true try typeof uint ulong unchecked unsafe ushort using "
    "var virtual void volatile while yield"
)
_JS_KEYWORDS = (
    "async await break case catch class const continue debugger default delete do else "
    "export extends finally for function if import in instanceof let new of return "
    "static super switch this throw try typeof var void while with yield true false null "
    "undefined"
)
_MERMAID_KEYWORDS = (
    "flowchart graph subgraph end classDef class click style linkStyle direction "
    "sequenceDiagram participant actor note over loop alt opt par rect activate "
    "deactivate stateDiagram stateDiagram-v2 erDiagram gantt journey pie "
    "gitGraph requirementDiagram C4Context TB TD BT RL LR"
)

_STR_DQ = r'"(?:\\.|[^"\\])*"'
_STR_SQ = r"'(?:\\.|[^'\\])*'"
_NUM = r"\b(?:0[xX][0-9a-fA-F_]+|\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?)\b"

SPECS: dict[str, list[tuple[str, str]]] = {
    "sh": [
        ("s", _STR_SQ),
        ("s", _STR_DQ),
        ("c", r"(?:^|(?<=\s))#[^\n]*"),
        ("v", r"\$\{[^}]*\}|\$[A-Za-z_][\w]*|\$[0-9@*#?]"),
        ("o", r"(?<![\w-])--?[A-Za-z][\w-]*"),
        ("k", _kw(_SH_KEYWORDS)),
        ("n", _NUM),
    ],
    "python": [
        ("s", r"(?s:[rRbBfFuU]{0,2}(?:\"\"\".*?\"\"\"|'''.*?'''))"),
        ("s", r"[rRbBfFuU]{0,2}(?:" + _STR_DQ + "|" + _STR_SQ + ")"),
        ("c", r"#[^\n]*"),
        ("f", r"(?<=\bdef\s)[A-Za-z_]\w*|(?<=\bclass\s)[A-Za-z_]\w*"),
        ("p", r"^\s*@[\w.]+"),
        ("k", _kw(_PY_KEYWORDS)),
        ("t", _kw(_PY_BUILTINS)),
        ("n", _NUM),
    ],
    "json": [
        ("p", _STR_DQ + r"(?=\s*:)"),
        ("s", _STR_DQ),
        ("k", r"\b(?:true|false|null)\b"),
        ("n", _NUM),
    ],
    "yaml": [
        ("c", r"(?:^|(?<=\s))#[^\n]*"),
        ("p", r"^[ \t]*(?:-[ \t]+)?[A-Za-z_][\w.\-/]*(?=[ \t]*:)"),
        ("s", _STR_DQ),
        ("s", _STR_SQ),
        ("v", r"(?<![\w])[&*][A-Za-z_][\w\-]*"),
        ("k", r"\b(?:true|false|null|yes|no|on|off|~)\b"),
        ("n", _NUM),
    ],
    "http": [
        ("k", r"^(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|TRACE)\b"),
        ("t", r"^HTTP/\d(?:\.\d)?\b"),
        ("p", r"^[A-Za-z][A-Za-z0-9\-]*(?=:)"),
        ("p", _STR_DQ + r"(?=\s*:)"),
        ("s", _STR_DQ),
        ("k", r"\b(?:true|false|null)\b"),
        ("n", _NUM),
    ],
    "powershell": [
        ("s", _STR_SQ),
        ("s", _STR_DQ),
        ("c", r"(?s:<#.*?#>)"),
        ("c", r"#[^\n]*"),
        ("v", r"\$[A-Za-z_][\w:]*|\$\{[^}]*\}"),
        ("f", r"\b[A-Z][a-zA-Z]+-[A-Z][a-zA-Z]+\b"),
        ("o", r"(?<![\w-])-[A-Za-z][\w]*"),
        ("k", "(?i:" + _kw(_PS_KEYWORDS) + ")"),
        ("n", _NUM),
    ],
    "sql": [
        ("c", r"--[^\n]*"),
        ("s", _STR_SQ),
        ("k", "(?i:" + _kw(_SQL_KEYWORDS) + ")"),
        ("n", _NUM),
    ],
    "ini": [
        ("c", r"(?:^|(?<=\s))[#;][^\n]*"),
        ("t", r"^\[[^\]\n]*\]"),
        ("p", r"^[ \t]*[A-Za-z_][\w.\-]*(?=[ \t]*=)"),
        ("s", _STR_DQ),
        ("s", _STR_SQ),
        ("n", _NUM),
    ],
    "xml": [
        ("c", r"(?s:<!--.*?-->)"),
        ("s", _STR_DQ),
        ("s", _STR_SQ),
        ("k", r"</?[A-Za-z][\w:.\-]*|/?>"),
        ("p", r"\b[A-Za-z_][\w:.\-]*(?==)"),
    ],
    "csharp": [
        ("s", r"(?s:@\"(?:[^\"]|\"\")*\")|\$?" + _STR_DQ),
        ("s", _STR_SQ),
        ("c", r"//[^\n]*"),
        ("c", r"(?s:/\*.*?\*/)"),
        ("p", r"^\s*\[[A-Za-z][\w.]*[^\]\n]*\]"),
        ("k", _kw(_CS_KEYWORDS)),
        ("n", _NUM),
    ],
    "javascript": [
        ("s", r"(?s:`(?:\\.|[^`\\])*`)"),
        ("s", _STR_DQ),
        ("s", _STR_SQ),
        ("c", r"//[^\n]*"),
        ("c", r"(?s:/\*.*?\*/)"),
        ("k", _kw(_JS_KEYWORDS)),
        ("n", _NUM),
    ],
    "markdown": [
        ("k", r"^#{1,6} [^\n]*"),
        ("t", r"^ {0,3}(?:```|~~~)[^\n]*"),
        ("s", r"`[^`\n]+`"),
        ("f", r"\[[^\]\n]*\]\([^)\n]*\)"),
        ("o", r"^ {0,3}(?:[-*+]|\d+[.)]) "),
        ("c", r"^ {0,3}> [^\n]*"),
    ],
    "mermaid": [
        ("c", r"%%[^\n]*"),
        ("s", _STR_DQ),
        ("o", r"-{1,3}>|={1,2}>|-\.->|---|\.\.\.|<-{1,3}|\|"),
        ("k", _kw(_MERMAID_KEYWORDS)),
        ("t", r"\[[^\]\n]*\]|\([^)\n]*\)|\{[^}\n]*\}"),
    ],
    "diff": [
        ("a", r"^\+[^\n]*"),
        ("d", r"^-[^\n]*"),
        ("c", r"^@@[^\n]*"),
    ],
}

_COMPILED: dict[str, re.Pattern[str]] = {}


def _compiled(language: str) -> re.Pattern[str] | None:
    if language in _COMPILED:
        return _COMPILED[language]
    spec = SPECS.get(language)
    if not spec:
        return None
    parts: list[str] = []
    for index, (klass, pattern) in enumerate(spec):
        parts.append(f"(?P<g{index}_{klass}>{pattern})")
    compiled = re.compile("|".join(parts), re.MULTILINE)
    _COMPILED[language] = compiled
    return compiled


def normalise(language: str) -> str:
    lowered = (language or "").strip().lower()
    return ALIASES.get(lowered, lowered)


def highlight(code: str, language: str) -> str:
    """Return escaped HTML for ``code``, with token spans when we know the language."""
    normalised = normalise(language)
    pattern = _compiled(normalised)
    if pattern is None:
        return esc_text(code)
    out: list[str] = []
    position = 0
    for match in pattern.finditer(code):
        if match.start() > position:
            out.append(esc_text(code[position : match.start()]))
        group = match.lastgroup or ""
        klass = group.rsplit("_", 1)[-1]
        if klass not in _CLASS_NAMES:
            klass = "o"
        out.append(f'<span class="t-{klass}">{esc_text(match.group(0))}</span>')
        position = match.end()
    if position < len(code):
        out.append(esc_text(code[position:]))
    return "".join(out)
