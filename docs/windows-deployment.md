# Windows deployment

How Project CHAOS is built, packaged and installed on Windows.

> **Scope note.** This document currently covers the **embedded Python runtime**
> — the `python\` tree that lets the product run on a machine with no Python
> installed. The .NET publish, MSI authoring, service registration and
> commissioning sections belong to the host/installer work and slot in around
> these; add them, do not replace them.

---

## Embedded Python runtime

### Why it exists

The product must not require Python to be preinstalled. Nobody is going to
babysit a Python install on a machine in a container next to a battery bank, and
"the operator upgraded Python and the control plane stopped starting" is not an
acceptable failure mode. So the product carries its own interpreter, built from
the official Windows embeddable CPython distribution.

The .NET gateway (`Chaos.Host`) supervises that interpreter as a child process.
`PythonRuntimeResolver` resolves exactly `<install root>\python\python.exe` and,
finding a `python\` directory without an interpreter in it, treats the install as
**damaged** and refuses to fall back to a system Python. That refusal is
deliberate: borrowing the machine's Python would run the control plane against
dependency versions nobody tested.

### Run these two commands

From a normal PowerShell prompt (Windows PowerShell 5.1 or PowerShell 7 — both
are supported), in `windows\build`:

```powershell
.\build.ps1 -Task runtime      # build it
.\verify-runtime.ps1           # prove it works
```

The first produces `windows\build\out\stage\python\`. The second runs ten checks
against that tree with the interpreter under test and prints `All 10 checks
passed.` or a `[FAIL]` line naming the likely cause.

`verify-runtime.ps1` also works against an installed product, which is how you
tell "the install is broken" from "the product is broken":

```powershell
.\verify-runtime.ps1 -Path "$env:ProgramFiles\Project CHAOS\python"
```

Requirements: outbound HTTPS **at build time only** (the installed product never
downloads anything), and no compiler — see *Wheels only*, below. Administrator
rights are not needed for either command.

### What gets produced

```
<install root>\python\
    python.exe                       <- the supervisor resolves THIS path
    pythonw.exe
    python311.dll   python3.dll
    python311.zip                    the stdlib, .pyc only, 649 modules
    python311._pth                   rewritten by the build; see below
    vcruntime140.dll                 shipped by CPython; no VC++ redist needed
    vcruntime140_1.dll
    libssl-3.dll  libcrypto-3.dll    OpenSSL 3, for TLS
    sqlite3.dll                      the product's database engine
    libffi-8.dll
    _socket.pyd  _ssl.pyd  _sqlite3.pyd  _ctypes.pyd  _decimal.pyd
    _asyncio.pyd  _hashlib.pyd  _queue.pyd  _overlapped.pyd  ...
    LICENSE.txt
    Lib\
      site-packages\
        sitecustomize.py             isolation; see below
        chaos\                       the platform, from a locally built wheel
          web\                       the operator UI, copied in explicitly
        simulator\
        fastapi\ starlette\ uvicorn\ sqlalchemy\ pydantic\ pydantic_core\
        paho\ yaml\ jsonschema\ httpx\ httpcore\ anyio\ greenlet\ ...
        __pycache__\ everywhere      precompiled, unchecked-hash
    Scripts\
      chaos.exe                      the operator CLI entry point
      homestead-simulator.exe
      uvicorn.exe  httpx.exe  jsonschema.exe  dotenv.exe  fastapi.exe
```

`Lib\site-packages` and `Scripts` are the standard Windows (`nt`) sysconfig
scheme for a prefix, so pip puts things there without being told.

**There is no `python\Lib\` beyond `site-packages`, no `DLLs\`, and no
`tcl\`** — an embeddable distribution keeps the stdlib in `python311.zip` and
puts every `.pyd` flat in the root. There is also no `venv` module and no
`ensurepip`. That is normal and is why the build works the way it does.

### The layout contract with the supervisor

`PythonRuntimeResolver.TryEmbedded` does two things:

1. Looks for `<install root>\python\python.exe`. The build produces exactly that.
2. If `<install root>\app\src\chaos\cli.py` **also** exists, it sets
   `PYTHONPATH=<install root>\app\src`.

Point 2 matters, because **a `._pth` interpreter discards `PYTHONPATH`**. In that
layout the supervisor would report that it is running the platform from
`app\src` while actually importing it from `Lib\site-packages`, and an operator
who edits `app\src` on the property to fix something would see no effect at all.

`build.ps1 -Task stage` stages `app\data`, `app\schemas`, `app\tools` and
`app\web` — **not** `app\src` — so the resolver takes its site-packages branch
and everything is consistent. Because that consistency was accidental rather
than enforced, `python-runtime.ps1` now fails the build if `app\src\chaos\cli.py`
appears in the staged tree. The resolver is tested and correct; the package is
what must not put it in that position.

The supervisor launches the backend as:

```
python.exe -m chaos.cli --log-level <level> serve --host <addr> --port <port>
```

so `python -m chaos.cli --help` is a check in its own right, and
`verify-runtime.ps1` runs it.

### The `._pth` file, and what it silently changes

An embeddable distribution ships `python311._pth`, which upstream contains:

```
python311.zip
.

# Uncomment to run site.main() automatically
#import site
```

The build rewrites it whole (not with a regex against upstream) to:

```
python311.zip
.
Lib\site-packages

# ... comment block ...
import site
```

From CPython 3.11.9 `Modules/getpath.py`, the presence of any `._pth` sets
`isolated = 1`, `use_environment = 0`, `site_import = 0` (unless a line reads
exactly `import site`) and `safe_path = 1`. In practice:

| Environment variable | Effect in this runtime |
| --- | --- |
| `PYTHONPATH` | **Ignored.** Nothing can inject a path through the environment. |
| `PYTHONHOME` | **Ignored.** |
| `PYTHONUNBUFFERED` | Honoured — read before `getpath` runs. |
| `PYTHONIOENCODING` | Honoured. |
| `PYTHONDONTWRITEBYTECODE` | Honoured. |
| `PYTHONUTF8` | Honoured (pre-config, earlier still). |

The last four matter because `BackendSupervisor.BuildStartSpec` sets three of
them. They were confirmed by running a real 3.11 interpreter under a `._pth`
file, not inferred from the documentation.

If you ever need to add a directory to the runtime's `sys.path`, add a line to
`._pth` or drop a `.pth` file in `Lib\site-packages`. Setting `PYTHONPATH` will
do nothing and will not warn you.

### Isolation from the operator's own Python

A `._pth` file cannot switch off the **per-user site directory**
(`%APPDATA%\Python\Python311\site-packages`), because that switch is read from
`-s`/`PYTHONNOUSERSITE` long before `getpath` runs. Left alone this bites twice:

* **At build time**, pip sees whatever the person at the keyboard once installed
  with `pip install --user` and treats it as already satisfied, so it does not
  put those packages in the tree. The tree ships incomplete and still passes its
  own import checks *on the build machine*.
* **At run time**, those same packages shadow the ones the product ships.

This is not hypothetical. On a build machine with `idna` and `certifi` in the
user site, pip installed neither into the tree, and `httpx` in the "finished"
runtime imported them out of the developer's profile.

So the build writes `Lib\site-packages\sitecustomize.py` **before pip runs**. It
removes the user site directory from `sys.path` and sets
`site.ENABLE_USER_SITE = False`, all inside `try/except` — a `sitecustomize` that
raises breaks every interpreter start. `verify-runtime.ps1` asserts the effect,
so if the mechanism ever stops working the build fails instead of shipping a
leaky tree.

### Wheels only

Every install of a named requirement passes `--only-binary=:all:`, so pip must
use a `cp311` `win_amd64` wheel and fails loudly if one does not exist.

This is not a preference. **This interpreter cannot build an sdist**:

* pip's default build isolation hands the build backend to a child interpreter
  through `PYTHONPATH` — which a `._pth` interpreter ignores.
* pip's alternative venv-based isolation needs `python -m venv`, and the
  embeddable distribution does not ship the `venv` module at all.

So a source build either fails with a message about `setuptools.build_meta` that
explains nothing, or — on a machine that happens to have Visual Studio, like the
one this is developed on — quietly succeeds and produces a locally compiled
binary that was never tested anywhere else.

**Every dependency of this project publishes a `win_amd64` wheel for CPython
3.11.** Checked against live PyPI, full transitive closure, resolved for
Windows:

| Compiled (`cp311-cp311-win_amd64`) | Pure Python (`py3-none-any`) |
| --- | --- |
| `greenlet` (via SQLAlchemy) | `annotated-doc`, `annotated-types`, `anyio`, `attrs` |
| `httptools` (via `uvicorn[standard]`) | `certifi`, `click`, `colorama`, `fastapi`, `h11` |
| `pydantic-core` | `httpcore`, `httpx`, `idna`, `jsonschema` |
| `PyYAML` | `jsonschema-specifications`, `paho-mqtt`, `pydantic` |
| `rpds-py` (via `jsonschema`) | `pydantic-settings`, `python-dotenv`, `referencing` |
| `SQLAlchemy` | `starlette`, `typing-extensions`, `typing-inspection` |
| `watchfiles`, `websockets` | `uvicorn`, and the `chaos` wheel itself |

The `postgres` extra (`-IncludePostgres`) is clean too: `psycopg`,
`psycopg-binary`, `alembic`, `Mako`, `MarkupSafe`, `tzdata`.

One dependency deserves a note. `uvicorn[standard]` pulls `uvloop`, which has
**no Windows wheel and never will** — it is a libuv binding for POSIX. That is
fine: the extra's marker is
`sys_platform != "win32" and sys_platform != "cygwin"`, so `uvloop` is simply not
part of the Windows dependency set. If you ever check this yourself with
`pip download --platform win_amd64`, you will see it fail on `uvloop`; that is a
**pip artefact**, because `--platform` selects wheel tags but does not re-evaluate
environment markers. Resolve with a tool that does cross-platform markers
properly (`uv pip compile --python-platform x86_64-pc-windows-msvc
--python-version 3.11`) and `uvloop` correctly disappears.

### How the project itself is installed

Not `pip install <repo>`. Two steps, and the order is the point:

1. `pip wheel --no-deps --no-build-isolation` builds a `chaos-*-py3-none-any.whl`
   from the checkout. This is the only build in the whole script; it is pure
   Python and needs no compiler.
2. `pip install --only-binary=:all: <that wheel>` installs it, which is what
   drags in fastapi, uvicorn, sqlalchemy, pydantic and the rest — all wheels.

Installing the checkout directly would put the local source tree and its whole
transitive dependency graph under one pip run, and `--only-binary=:all:` cannot
be used there because the checkout *is* a source tree.

setuptools writes `build\` and `src\chaos.egg-info\` into the checkout while
building that wheel. `build\` is not in `.gitignore`, so the script removes it
again if it was not there before.

### The operator UI assets are copied in by hand, on purpose

`src/chaos/web/` is not a Python package (no `__init__.py`) and `pyproject.toml`
declares no package-data, so **the wheel contains zero web assets** (verified:
the built wheel's only top-level entries are `chaos`, `simulator` and the
`.dist-info`). But `chaos/api/app.py` computes `WEB_DIR` as
`<package dir>/../web` and only mounts `/ui` when that directory exists.

Without the explicit copy, the operator UI silently does not exist: the API
answers, `/ui` returns 404, and nothing logs an error. `verify-runtime.ps1`
resolves `WEB_DIR` from the *installed* package and checks the files are there.

### Relocatable console scripts

pip writes an **absolute** interpreter path into every console-script `.exe` it
creates, taken from `sys.executable` — i.e. the staging tree. The MSI then moves
everything to `%ProgramFiles%\Project CHAOS\`, where that path does not exist,
and every launcher dies with:

```
Fatal error in launcher: Unable to create process using
'"...\out\stage\python\python.exe" "...\Scripts\chaos.exe"'
```

Nothing catches this before a customer does, because in the staging tree the
path is still valid — the build's own checks pass, and so does `-Task smoke`,
which runs from the staging directory.

So `relocate-launchers.py` rewrites each shebang to the launcher's own
documented relative form:

```
#!<launcher_dir>\"..\python.exe"
```

The distlib stub pip vendors is built with `SUPPORT_RELATIVE_PATH`, resolves that
token against the directory the `.exe` is in, and quotes the result before
`CreateProcessW`, so a path with spaces is fine. GUI scripts keep `pythonw.exe`;
any interpreter arguments in the original shebang are preserved.
`verify-runtime.ps1` fails if any launcher still holds an absolute path.

### How long it takes, and how big it is

**Downloads (build machine, once per `-Force` run):**

| Artefact | Bytes | Cached? |
| --- | ---: | --- |
| `python-3.11.9-embed-amd64.zip` | 11,249,023 | yes, in `out\downloads` |
| `pip-26.2.1-py3-none-any.whl` | 1,816,632 | yes, in `out\downloads` |
| 31 dependency wheels | ~7.3 MB | **no** — `--no-cache-dir` is deliberate |

The dependency wheels are re-fetched every run. That is the price of not
shipping whatever a stale pip cache happens to hold.

**Time.** The interpreter work itself is small: on a machine with a good
connection the pip steps take well under a minute end to end, and `compileall`
a couple of seconds. Budget **2–5 minutes** for a first `-Task runtime` on
Windows and expect Defender's real-time scanning to dominate — it inspects every
one of the ~3,000 files pip writes. If it is much slower than that, exclude
`windows\build\out` from real-time scanning on the build machine (not on the
target machine).

**Size.** Derived from the actual extracted embeddable and the actual
`win_amd64` wheel contents:

| Part | Size |
| --- | ---: |
| Extracted embeddable distribution | 20.7 MiB |
| `Lib\site-packages` from wheels | 24.1 MiB (17.1 native, 7.0 pure) |
| Operator UI assets | 0.4 MiB |
| `.pyc` from `compileall` | ~6 MiB |
| **Shipped `python\` tree** | **≈ 55 MB** |

`setuptools`, `wheel`, `pip` and `pkg_resources` are installed and then pruned;
`-NoPrune` keeps them and adds roughly 15 MB. `-IncludePostgres` adds about
26 MB, most of it the bundled libpq in `psycopg-binary`. The build prints the
exact figure it produced.

### When it fails

| What you see | What it means |
| --- | --- |
| `SHA-256 verification failed twice` | Either the pin in `python-runtime.lock.json` is stale, or the download was tampered with. **Do not work around it.** There is no `--skip-hash` switch and there should never be one — this artefact becomes the interpreter that decides whether a pump starts. Re-pin by following the provenance procedure written into the lock file: fetch the artefact and its `.asc`, `gpg --verify`, then take the SHA-256 of the verified bytes. |
| `Could not find a version that satisfies the requirement <x>` during a `--only-binary=:all:` install | There is no `cp311 win_amd64` wheel for `<x>`. A new or upgraded dependency has broken the embedded-runtime approach for that package. Pin it to a version that ships a wheel, or drop it. Do not remove `--only-binary=:all:`. |
| `<Destination> already exists` | Re-run with `-Force`, or use `.\build.ps1 -Task runtime`, which passes it. The script refuses to build on top of a partial tree on purpose. |
| `python.exe is not in the extracted tree` | The embeddable zip layout changed. `<install root>\python\python.exe` is a contract with the supervisor; nothing downstream works without it. |
| `Expected stdlib archive python311.zip is missing` | The `tag` field for that version in the lock file does not match the distribution. |
| `[FAIL] isolated from the per-user site directory` | `sitecustomize.py` was removed or shadowed. Rebuild; do not hand-repair. Anything the operator installed with `pip install --user` is currently able to shadow a shipped dependency. |
| `[FAIL] compiled extension modules load` — "DLL load failed" | A wheel for the wrong platform, or a missing `vcruntime140.dll` / `vcruntime140_1.dll`. Both ship inside the embeddable zip; if they are gone, check the antivirus quarantine log for the runtime directory before blaming the build. |
| `[FAIL] console-script launchers are relocatable` | `relocate-launchers.py` did not run. The tree works only where it was built. Rebuild. |
| `Fatal error in launcher: Unable to create process` from `chaos.cmd` on an installed machine | Same cause, discovered later. Rebuild the runtime and repackage; the installed tree cannot be repaired in place. |
| `The staged tree contains ...\app\src\chaos\cli.py` | Something started staging the platform source next to the runtime. The supervisor would then set a `PYTHONPATH` this interpreter ignores. Stage `app\data`, `app\schemas`, `app\tools` and `app\web` only. |
| `compileall reported errors` (a warning, not a failure) | Some third-party file did not compile — usually a vendored Python-2 fixture or a template. The offending files are printed. It is only a problem if one of them is ours; those modules just compile at first import. |

The script is re-runnable. Downloads are cached and re-verified on every run; a
cached file whose digest no longer matches is deleted and re-fetched once, and a
second mismatch is a hard failure.

### Changing the pinned Python version

`python-runtime.lock.json` also carries verified pins for 3.12.10 and 3.13.15.
Moving up is a one-line change (`"default"`) plus a matching entry in the Python
CI matrix in `.github/workflows/ci.yml` — a control system ships the
configuration that was tested, not the newest one.

Read the note in the lock file before a long deployment: **3.11.9 is the last
CPython 3.11 with Windows binaries.** 3.11.10 and later are source-only security
releases, so this embeddable will never receive a python.org security update.

If you change the version, also change `EXPECTED_PYTHON` in
`windows\build\verify-runtime.py`, and re-run the wheel-availability check for
the new interpreter — the table above is specific to `cp311`.

### What has and has not been proven

Verified by execution, on Linux, against the real artefacts:

* Both lock-file pins — SHA-256 **and** byte size — match the live artefacts.
* The embeddable zip's contents: `python311._pth` exists and ships with
  `import site` commented out; `python311.zip` is 649 `.pyc` modules with no
  `venv` and no `ensurepip`; both `vcruntime140.dll` and `vcruntime140_1.dll`
  are present.
* `._pth` semantics, on a real 3.11 interpreter: `PYTHONPATH` discarded,
  isolated mode on, `import site` required for `site.main()`, comment lines
  ignored, `PYTHONUNBUFFERED`/`PYTHONIOENCODING`/`PYTHONDONTWRITEBYTECODE` still
  honoured.
* The pip-from-a-wheel bootstrap works under a `._pth` interpreter.
* Every dependency, full transitive closure, has a `cp311 win_amd64` wheel.
* The whole install sequence, run end to end into a `._pth` tree: pip bootstrap,
  wheels-only dependency install, `pip wheel` of the project, install of that
  wheel, web-asset copy, `compileall`, and all ten verification checks passing.
* The user-site defect and its fix: reproduced (pip skipped `idna` and
  `certifi`), then fixed with `sitecustomize.py` and re-run clean.
* The launcher rewrite: byte-exact on launchers synthesised the way distlib
  builds them; stub bytes unchanged, appended zip still parses, quoted and
  unquoted shebangs, `pythonw.exe` preserved, interpreter arguments preserved,
  idempotent on a second run.
* The PowerShell 5.1 quoting defect, reproduced with
  `$PSNativeCommandArgumentPassing = 'Legacy'`, and the file-based fix working
  in both modes including a path with spaces.
* All four PowerShell files parse cleanly; the prune wildcard-escaping fix
  demonstrably matches where the old form matched nothing.

**Not proven, and only Windows can prove it:** that `python.exe` from the
embeddable distribution runs the sequence above (it was run with a real CPython
3.11 on Linux under an equivalent `._pth`, not with the shipped `python.exe`);
that the relocated launchers execute — the byte layout is right and the stub's C
source has the code path, but no `.exe` has actually been launched; and the wall
clock and on-disk figures above, which are derived rather than measured on
Windows. Run the two commands at the top of this section and the answer is
immediate.
