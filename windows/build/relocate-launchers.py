"""Make the console-script launchers in an embedded runtime relocatable.

WHY THIS EXISTS
---------------
pip installs a console script on Windows as a small ``.exe``: the distlib
"Simple Launcher" stub, then a UTF-8 shebang line, then a zip holding
``__main__.py``::

    <launcher .exe bytes> #!"C:\\path\\to\\python.exe"\\n PK\\x03\\x04...

pip takes that interpreter path from ``sys.executable`` -- the interpreter that
was running pip -- and writes it as an ABSOLUTE path.  For us that is the
staging tree, e.g.::

    ...\\windows\\build\\out\\stage\\python\\python.exe

The MSI then installs the same bytes to ``%ProgramFiles%\\Project CHAOS\\``,
where that path does not exist.  Every launcher in ``python\\Scripts`` dies with

    Fatal error in launcher: Unable to create process using
    '"...\\out\\stage\\python\\python.exe" "...\\Scripts\\chaos.exe"'

and nothing catches it before the customer does: the staged tree passes every
build-time check because in the staging tree the path is still valid.

THE FIX
-------
The launcher supports a relocatable shebang.  From simple_launcher's
``launcher.c`` (the stub pip vendors, built with ``SUPPORT_RELATIVE_PATH``)::

    #define RELATIVE_PREFIX L"<launcher_dir>\\"
    ...
    if (!_wcsnicmp(RELATIVE_PREFIX, wcp, prefix_offset)) {
        wcscpy_s(dbuffer, MAX_PATH, script_path);
        PathRemoveFileSpecW(dbuffer);          /* dir holding the .exe */
        if (wcp[prefix_offset] == L'"') { ...  /* optional quoted remainder */ }
        PathCombineW(pbuffer, dbuffer, &wcp[prefix_offset]);
    }

So a shebang of::

    #!<launcher_dir>\\"..\\python.exe"

resolves at run time against the directory the ``.exe`` is sitting in.  The
launchers live in ``<runtime>\\Scripts``, so ``..\\python.exe`` is
``<runtime>\\python.exe`` -- wherever the tree ends up, including a path with
spaces, because PathCombineW does the join and the launcher quotes the result
before CreateProcessW.

WHAT THIS DOES NOT DO
---------------------
It does not update the ``RECORD`` hash in the owning ``.dist-info``.  Nothing at
run time verifies RECORD (``importlib.metadata`` reads METADATA and
entry_points, not hashes), and pip -- the only tool that would care -- is pruned
out of the shipped tree.  Recording that here so the next person does not spend
an afternoon on it.

Usage:  python.exe relocate-launchers.py <runtime root>
Exit 0 on success; prints one line per launcher.  Exit non-zero, with the reason
on stderr, if any launcher does not have the structure described above -- a
silent skip here is a broken operator CLI on the property.
"""

from __future__ import annotations

import sys
from pathlib import Path

# What the launcher's own C code looks for.  Keep the trailing backslash: the
# stub compares RELATIVE_PREFIX_LENGTH == 15 characters, which includes it.
RELATIVE_PREFIX = "<launcher_dir>\\"

ZIP_MAGIC = b'PK\x03\x04'
SHEBANG_MAGIC = b'#!'

# The only interpreters a launcher may point at.  pip writes sys.executable, so
# in a healthy tree this is always one of these two; anything else means the
# assumptions in this file no longer hold and we would rather stop than write a
# plausible-looking wrong path into a binary.
#
# console_scripts get python.exe (distlib t64.exe), gui_scripts get pythonw.exe
# (w64.exe).  The distinction is preserved: pointing a gui_script at python.exe
# would pop a console window on a machine whose whole point is that nobody is
# sitting at it.
ALLOWED_INTERPRETERS = ('python.exe', 'pythonw.exe')

# A shebang is one line holding one path.  Anything longer than this is not a
# shebang and we would rather fail than write into the middle of a binary.
MAX_SHEBANG_BYTES = 1024


def find_shebang(data: bytes) -> tuple[int, int, str]:
    """Locate the shebang that precedes the appended zip.

    Returns ``(start, end, text)`` where ``data[start:end]`` is the whole
    shebang INCLUDING its trailing newline, and ``text`` is the line without it.
    Raises ValueError if the file does not have the launcher layout.
    """
    zip_at = data.find(ZIP_MAGIC)
    while zip_at != -1:
        # The shebang ends with the newline immediately before the zip.
        if zip_at > 0 and data[zip_at - 1:zip_at] == b'\n':
            newline_at = zip_at - 1
            window_start = max(0, newline_at - MAX_SHEBANG_BYTES)
            hash_bang = data.rfind(SHEBANG_MAGIC, window_start, newline_at)
            if hash_bang != -1:
                candidate = data[hash_bang:newline_at]
                # A real shebang is a single line of text.  If it holds a NUL or
                # a newline we found machine code that happens to contain "#!",
                # not a shebang.
                if b'\x00' not in candidate and b'\n' not in candidate:
                    try:
                        text = candidate.decode('utf-8')
                    except UnicodeDecodeError:
                        pass
                    else:
                        return hash_bang, zip_at, text
        zip_at = data.find(ZIP_MAGIC, zip_at + 1)

    raise ValueError(
        'no "#!<interpreter>\\n" immediately before an appended zip archive; '
        'this does not look like a pip/distlib console-script launcher'
    )


def split_shebang(text: str) -> tuple[str, str]:
    """Split ``#!<executable><rest>`` into (executable, rest).

    Mirrors the stub's own ``find_exe_extension``: the executable token is
    either double-quoted, or runs to the first ``.exe`` that is followed by end
    of line, a quote or whitespace.
    """
    body = text[len(SHEBANG_MAGIC.decode()):].lstrip()
    if body.startswith('"'):
        closing = body.find('"', 1)
        if closing == -1:
            raise ValueError(f'unterminated quote in shebang {text!r}')
        return body[1:closing], body[closing + 1:]

    lowered = body.lower()
    search_from = 0
    while True:
        dot_exe = lowered.find('.exe', search_from)
        if dot_exe == -1:
            raise ValueError(f'no ".exe" in shebang {text!r}')
        end = dot_exe + 4
        following = body[end:end + 1]
        if following == '' or following == '"' or following.isspace():
            return body[:end], body[end:]
        search_from = end


def relocate(path: Path) -> str:
    data = path.read_bytes()
    start, end, old = find_shebang(data)

    executable, rest = split_shebang(old)
    if executable.startswith(RELATIVE_PREFIX):
        return f'already relative  {path.name}  {old.strip()}'

    interpreter = executable.replace('/', '\\').rsplit('\\', 1)[-1]
    if interpreter.lower() not in ALLOWED_INTERPRETERS:
        raise ValueError(
            f'launcher points at {interpreter!r}, not one of '
            f'{", ".join(ALLOWED_INTERPRETERS)} (shebang was {old.strip()!r})'
        )

    # <runtime>\Scripts\<name>.exe  ->  ..\<interpreter>  ==  <runtime>\<interpreter>
    new_text = f'{SHEBANG_MAGIC.decode()}{RELATIVE_PREFIX}"..\\{interpreter}"{rest}'
    patched = data[:start] + new_text.encode('utf-8') + b'\n' + data[end:]
    path.write_bytes(patched)
    return f'rewrote           {path.name}  {old.strip()}  ->  {new_text.strip()}'


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2

    root = Path(argv[1])
    scripts = root / 'Scripts'
    if not scripts.is_dir():
        # No console scripts at all is a real failure: the operator CLI is one.
        print(f'ERROR: no Scripts directory under {root}', file=sys.stderr)
        return 1

    exes = sorted(p for p in scripts.iterdir() if p.suffix.lower() == '.exe')
    if not exes:
        print(f'ERROR: no .exe launchers in {scripts}', file=sys.stderr)
        return 1

    failures = []
    for exe in exes:
        try:
            print('    ' + relocate(exe))
        except (ValueError, OSError) as exc:
            failures.append(f'{exe.name}: {exc}')

    for failure in failures:
        print(f'ERROR: could not make {failure}', file=sys.stderr)

    if failures:
        print(
            'ERROR: the shipped tree would only work from the directory it was '
            'built in.  Do not package it.',
            file=sys.stderr,
        )
        return 1

    print(f'    {len(exes)} launcher(s) now resolve python.exe relative to their own directory')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
