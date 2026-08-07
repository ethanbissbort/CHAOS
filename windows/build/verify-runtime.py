"""Check that a built embedded Python runtime is actually usable.

Run it WITH THE INTERPRETER UNDER TEST, not with any other Python::

    <runtime>\\python.exe windows\\build\\verify-runtime.py

Every check prints one line.  A failure prints the likely cause, because the
person running this is trying to find out why the control plane will not start,
not to admire a stack trace.

This file is deliberately dependency-free and stdlib-only: it has to be able to
run inside a runtime whose dependencies are the thing in question.
"""

from __future__ import annotations

import os
import subprocess
import sys
import traceback
from pathlib import Path

# The import package and CLI module the .NET supervisor launches.  Keep these in
# one place: BackendSupervisor.BuildStartSpec runs
#     python.exe -m chaos.cli --log-level <level> serve --host <a> --port <p>
# so if this module cannot be imported and run, the product does not start.
PACKAGE = 'chaos'
CLI_MODULE = 'chaos.cli'

EXPECTED_PYTHON = (3, 11)

# What the platform imports at start-up.  A missing entry here is a service that
# comes up, logs an ImportError and dies, which on a homestead reads as "the
# lights on the panel went out".
REQUIRED_IMPORTS = [
    PACKAGE,
    CLI_MODULE,
    f'{PACKAGE}.api.app',
    'simulator',
    'fastapi',
    'starlette',
    'uvicorn',
    'sqlalchemy',
    'pydantic',
    'pydantic_settings',
    'yaml',
    'jsonschema',
    'paho.mqtt',
    'paho.mqtt.client',
    'httpx',
]

# Compiled extension modules.  These are the ones that a wrong-platform or
# source-built wheel breaks, and they fail at IMPORT time with a DLL error
# rather than at install time, so they get their own check.
REQUIRED_EXTENSIONS = [
    ('pydantic_core._pydantic_core', 'pydantic-core (Rust; needs the cp311 win_amd64 wheel)'),
    ('greenlet._greenlet', 'greenlet, pulled in by SQLAlchemy (C; cp311 win_amd64 wheel)'),
    ('_sqlite3', 'CPython sqlite3 extension + sqlite3.dll from the embeddable zip'),
    ('_ssl', 'CPython _ssl.pyd + libssl-3.dll/libcrypto-3.dll from the embeddable zip'),
    ('_socket', 'CPython _socket.pyd from the embeddable zip'),
    ('_ctypes', 'CPython _ctypes.pyd + libffi-8.dll from the embeddable zip'),
]

# Files the operator UI needs.  chaos/api/app.py computes WEB_DIR as
# <package dir>/../web and only mounts /ui when it exists, so a missing tree
# here is a UI that silently does not exist: the API answers and /ui 404s.
WEB_ASSETS = ['index.html', 'annunciator.html', 'annunciator.js', 'app.js', 'styles.css']

RELATIVE_PREFIX = '<launcher_dir>\\'


class Failure(Exception):
    """A check failed.  The message is the cause, phrased for an operator."""


_results: list[tuple[bool, str, str]] = []


def check(name):
    def decorate(fn):
        def run():
            try:
                detail = fn() or ''
                _results.append((True, name, detail))
            except Failure as exc:
                _results.append((False, name, str(exc)))
            except Exception:
                _results.append((False, name, 'unexpected error:\n' + traceback.format_exc()))
        run.__name__ = fn.__name__
        return run
    return decorate


# ---------------------------------------------------------------------------


@check('interpreter version')
def check_version():
    got = sys.version_info[:3]
    if got[:2] != EXPECTED_PYTHON:
        raise Failure(
            f'this runtime is Python {".".join(map(str, got))}, expected '
            f'{EXPECTED_PYTHON[0]}.{EXPECTED_PYTHON[1]}.x.\n'
            '      CAUSE: python-runtime.ps1 was run with -PythonVersion pointing at a '
            'different pin, or an older tree was not rebuilt with -Force.'
        )
    return f'Python {".".join(map(str, got))} at {sys.executable}'


@check('embeddable layout (._pth)')
def check_pth():
    root = Path(sys.executable).parent
    pth = sorted(root.glob('python*._pth'))
    if not pth:
        raise Failure(
            f'no python*._pth in {root}.\n'
            '      CAUSE: this is not an embeddable distribution, or the file was deleted. '
            'Without it the interpreter searches the registry and PYTHONPATH and is no '
            'longer isolated from any other Python on the machine.'
        )
    text = pth[0].read_text(encoding='ascii', errors='replace')
    if 'import site' not in text:
        raise Failure(
            f'{pth[0].name} does not contain an "import site" line.\n'
            '      CAUSE: site is disabled, so .pth files in Lib\\site-packages are not '
            'processed. python-runtime.ps1 writes this file whole; it has been edited or '
            'the upstream file was left in place.'
        )
    site_packages = root / 'Lib' / 'site-packages'
    if str(site_packages) not in sys.path:
        raise Failure(
            f'{site_packages} is not on sys.path.\n'
            f'      CAUSE: the "Lib\\site-packages" line is missing from {pth[0].name}. '
            'Nothing the product ships is importable without it.'
        )
    if not sys.flags.isolated:
        raise Failure(
            'the interpreter is not in isolated mode, which a ._pth file always sets.\n'
            '      CAUSE: the ._pth file was not read -- check that it is named exactly '
            'after the executable (python311._pth next to python.exe).'
        )
    return f'{pth[0].name} read; isolated mode on; site-packages on sys.path'


@check('PYTHONPATH is ignored (by design)')
def check_pythonpath_ignored():
    # This is not a defect, it is the contract, and it is worth asserting because
    # it is invisible: a ._pth file makes CPython discard PYTHONPATH entirely
    # (Modules/getpath.py: "if pth_dir: use_environment = 0 ... pythonpath = []").
    # Anything that expects to inject a path through PYTHONPATH -- including the
    # supervisor's development-layout code path -- will be silently ignored here.
    leaked = os.environ.get('PYTHONPATH')
    if leaked and any(entry and entry in sys.path for entry in leaked.split(os.pathsep)):
        raise Failure(
            'a PYTHONPATH entry reached sys.path, which a ._pth interpreter should '
            'never allow.\n'
            '      CAUSE: the ._pth file is not being read. See the previous check.'
        )
    return 'confirmed: sys.path comes from the ._pth file only'


@check('isolated from the per-user site directory')
def check_user_site():
    import site
    user_site = site.getusersitepackages()
    if isinstance(user_site, str) and user_site in sys.path:
        raise Failure(
            f'{user_site} is on sys.path.\n'
            '      CAUSE: Lib\\site-packages\\sitecustomize.py is missing or was shadowed. '
            'A ._pth file cannot switch off the user site directory, so anything the '
            'operator ever installed with "pip install --user" can shadow a shipped '
            'dependency and change what the control plane runs.'
        )
    return 'the per-user site directory is not on sys.path'


@check('platform and dependency imports')
def check_imports():
    import importlib
    failed = []
    for module in REQUIRED_IMPORTS:
        try:
            importlib.import_module(module)
        except Exception as exc:
            failed.append(f'{module}: {type(exc).__name__}: {exc}')
    if failed:
        raise Failure(
            'these modules could not be imported:\n        ' + '\n        '.join(failed) +
            f'\n      CAUSE: if {PACKAGE} itself is missing, the pip install of the project '
            'wheel did not run or did not land in Lib\\site-packages. If a third-party '
            'module is missing, its wheel was skipped -- re-run python-runtime.ps1 and read '
            'the pip output.'
        )
    package = importlib.import_module(PACKAGE)
    return f'{PACKAGE} {getattr(package, "__version__", "?")} and {len(REQUIRED_IMPORTS) - 1} more'


@check('compiled extension modules load')
def check_extensions():
    import importlib
    failed = []
    for module, why in REQUIRED_EXTENSIONS:
        try:
            importlib.import_module(module)
        except Exception as exc:
            failed.append(f'{module} -- {why}\n          {type(exc).__name__}: {exc}')
    if failed:
        raise Failure(
            'these compiled modules did not load:\n        ' + '\n        '.join(failed) +
            '\n      CAUSE: a wheel for the wrong platform or Python version, or a missing '
            'vcruntime140.dll/vcruntime140_1.dll. The embeddable zip ships both; if the '
            'prune step or an antivirus removed them, native wheels stop loading with an '
            '"ImportError: DLL load failed" that names nothing useful.'
        )
    return f'{len(REQUIRED_EXTENSIONS)} native modules imported'


@check('operator UI assets')
def check_web():
    import importlib
    try:
        app = importlib.import_module(f'{PACKAGE}.api.app')
    except ImportError as exc:
        raise Failure(
            f'could not import {PACKAGE}.api.app ({exc}).\n'
            '      CAUSE: see the import check above; there is nothing to say about the '
            'web assets until the package itself imports.'
        ) from None
    web_dir = Path(app.WEB_DIR)
    if not web_dir.is_dir():
        raise Failure(
            f'WEB_DIR does not exist: {web_dir}\n'
            f'      CAUSE: src/{PACKAGE}/web/ is not a Python package and pyproject.toml '
            'declares no package-data, so it is NOT in the wheel. python-runtime.ps1 copies '
            'it in explicitly; that step did not run. /ui will 404 and nothing will log it.'
        )
    missing = [name for name in WEB_ASSETS if not (web_dir / name).is_file()]
    if missing:
        raise Failure(
            f'missing under {web_dir}: {", ".join(missing)}\n'
            '      CAUSE: an incomplete copy of the web tree.'
        )
    return f'{len(WEB_ASSETS)} assets present under {web_dir}'


@check(f'python -m {CLI_MODULE} --help')
def check_cli_module():
    # Exactly how the supervisor starts the backend:
    #   python.exe -m chaos.cli --log-level INFO serve --host .. --port ..
    # "-m" has failure modes that a plain import does not (a package without a
    # runnable module, a broken __main__ guard), so it gets its own subprocess.
    completed = subprocess.run(
        [sys.executable, '-m', CLI_MODULE, '--help'],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise Failure(
            f'exited {completed.returncode}.\n'
            f'      CAUSE: the supervisor launches the backend with exactly this form '
            f'(python.exe -m {CLI_MODULE} ... serve ...), so the product cannot start.\n'
            '      stdout: ' + (completed.stdout or '').strip()[:500] + '\n'
            '      stderr: ' + (completed.stderr or '').strip()[:500]
        )
    return f'exit 0, {len(completed.stdout.splitlines())} lines of help'


@check('console-script launchers are relocatable')
def check_launchers():
    scripts = Path(sys.executable).parent / 'Scripts'
    if not scripts.is_dir():
        raise Failure(
            f'no {scripts} directory.\n'
            '      CAUSE: pip installed nothing, or the prune step removed too much. '
            'The operator CLI shim (chaos.cmd) calls into this directory.'
        )
    exes = sorted(p for p in scripts.iterdir() if p.suffix.lower() == '.exe')
    if not exes:
        raise Failure(f'no .exe launchers in {scripts}.')

    absolute = []
    for exe in exes:
        data = exe.read_bytes()
        zip_at = data.find(b'PK\x03\x04')
        hash_bang = data.rfind(b'#!', max(0, zip_at - 1024), zip_at) if zip_at > 0 else -1
        if hash_bang == -1:
            continue
        shebang = data[hash_bang:zip_at].decode('utf-8', 'replace').strip()
        if RELATIVE_PREFIX not in shebang:
            absolute.append(f'{exe.name}: {shebang}')

    if absolute:
        raise Failure(
            'these launchers embed an absolute interpreter path:\n        ' +
            '\n        '.join(absolute) +
            '\n      CAUSE: windows/build/relocate-launchers.py did not run. These .exe '
            'files work only from the directory they were BUILT in; once the MSI installs '
            'the tree under %ProgramFiles% every one of them fails with "Fatal error in '
            'launcher: Unable to create process using ...".'
        )
    return f'{len(exes)} launcher(s) resolve the interpreter relative to their own directory'


@check('console script runs')
def check_console_script():
    if os.name != 'nt':
        return 'skipped: not Windows'
    scripts = Path(sys.executable).parent / 'Scripts'
    candidates = [scripts / f'{PACKAGE}.exe']
    exe = next((c for c in candidates if c.is_file()), None)
    if exe is None:
        available = ', '.join(sorted(p.name for p in scripts.glob('*.exe'))) or '(none)'
        raise Failure(
            f'{candidates[0]} not found. Present: {available}\n'
            f'      CAUSE: pyproject.toml [project.scripts] no longer declares a "{PACKAGE}" '
            'entry point, or the install did not create it. chaos.cmd calls this .exe, so '
            'the operator CLI on a headless node stops working.'
        )
    completed = subprocess.run([str(exe), '--version'], capture_output=True, text=True)
    if completed.returncode != 0:
        raise Failure(
            f'{exe.name} --version exited {completed.returncode}.\n'
            '      CAUSE: if the message mentions "Fatal error in launcher", the embedded '
            'shebang points at an interpreter path that does not exist on this machine.\n'
            '      stderr: ' + (completed.stderr or '').strip()[:500]
        )
    return f'{exe.name} --version -> {(completed.stdout or "").strip()[:80]}'


CHECKS = [
    check_version,
    check_pth,
    check_pythonpath_ignored,
    check_user_site,
    check_imports,
    check_extensions,
    check_web,
    check_cli_module,
    check_launchers,
    check_console_script,
]


def main() -> int:
    print(f'Verifying the embedded runtime at {Path(sys.executable).parent}')
    print()
    for run in CHECKS:
        run()

    failed = 0
    for ok, name, detail in _results:
        marker = '[ ok ]' if ok else '[FAIL]'
        print(f'{marker} {name}')
        if detail:
            for line in detail.splitlines():
                print(f'       {line}')
        if not ok:
            failed += 1

    print()
    if failed:
        print(f'{failed} of {len(_results)} checks FAILED. This runtime must not be packaged.')
        return 1
    print(f'All {len(_results)} checks passed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
