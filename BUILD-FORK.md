# Building the OnionPress fork (arm64 DMG) for the moss demo

This fork (`Symbiosis-Lab/onionpress`) ships the moss integration: a static-first
Apache conf and the moss-receiver mu-plugin injected at **provision time** (no
WordPress image rebuild — Brewster's published images are reused unchanged), a
headless `onionname` CLI moss drives, and a self-updater repointed at this fork.

Because of runtime-injection, **you do not build or host any Docker images**. You
only build the macOS app bundle + DMG. Everything below is about that.

## What the DMG contains

`build/build-dmg-simple.sh` assembles `OnionPress.app` from `app/` source and:

- compiles the Swift launcher wrapper (universal),
- downloads + `lipo`s pinned universal container binaries (colima, lima, docker,
  docker-compose),
- builds `mkp224o` (universal, for vanity `op2…` onion addresses) — **required**;
  the build aborts if it can't be produced,
- freezes the Python MenubarApp with **py2app**,
- ad-hoc-signs the bundle and packages a compressed DMG at `build/onionpress.dmg`.

The result is a self-contained app: the end user never installs Python/Docker.

## Prerequisites (Apple Silicon Mac)

The DMG can only be built on **macOS on Apple Silicon (arm64)** — it uses
`hdiutil`, `py2app`, `lipo`, `codesign`, `swiftc`.

1. **Xcode Command Line Tools** — `swiftc`, `codesign`, `lipo`, `otool`.
   ```sh
   xcode-select --install
   ```

2. **Python 3.14** for py2app. Two supported sources (the script auto-detects, in
   this order):
   - **python.org universal2 Python 3.14** at
     `/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14`.
     Required for a **universal** build that also runs on Intel. Install from
     <https://www.python.org/downloads/> (the macOS universal2 installer).
   - **uv-managed Python 3.14** (single-arch). Fine for the **arm64-only demo
     build** (this fork's locked target). Install uv, then let the script pull
     3.14:
     ```sh
     curl -LsSf https://astral.sh/uv/install.sh | sh
     uv python install 3.14
     ```
   > Do **not** rely on `/usr/bin/python3` — on macOS 13/14 it is 3.9, and
   > `src/onionpress/` uses PEP 604 `X | None` syntax that fails to import there.
   > py2app freezes bytecode against the build interpreter, so a 3.9 build ships an
   > app that crashes on launch.

3. **Homebrew + mkp224o build deps** (for the `mkp224o` universal cross-compile):
   ```sh
   brew install libsodium autoconf automake
   ```
   These are only needed on a **cold** build. Once `mkp224o` is built it is cached
   under `build/.cache/bin/mkp224o-v1.7.0-universal`, and subsequent builds skip
   the libsodium cross-compile entirely (cache hit).

4. **Network access** — the script downloads pinned release assets from GitHub,
   docker.com, and libsodium.org on the first (uncached) build.

5. **`gh` CLI, authenticated** (`gh auth login`) — only needed to cut a release
   with `build/release.sh`.

### Pinned versions (from `build/build-dmg-simple.sh`)

| Component        | Version    |
|------------------|------------|
| Colima           | `v0.8.1`   |
| Lima             | `2.0.3`    |
| Docker CLI       | `27.5.1`   |
| Docker Compose   | `v2.40.2`  |
| mkp224o          | `v1.7.0`   |
| Python (py2app)  | `3.14`     |

## Build the DMG

```sh
cd /path/to/onionpress            # this fork's checkout
bash build/build-dmg-simple.sh
```

Output: `build/onionpress.dmg`. First cold build takes ~10–25 min (throttled
GitHub downloads + mkp224o/libsodium cross-compile + py2app); cached rebuilds are
much faster.

### Record the DMG hash

moss pins the artifact by sha256 (Track ACQ reads it from
`plugins/onionpress/stack-manifest.json` in the **moss** repo):

```sh
shasum -a 256 build/onionpress.dmg
```

## Cut a fork release

`build/release.sh` reads the version from `src/menubar.py`, builds the Linux
`.deb` (via `build-linux.sh`) **and** the macOS `.dmg`, then creates/updates the
GitHub release and uploads both.

```sh
# 1. Bump first (updates all version locations), commit.
build/bump-version.sh X.Y.Z

# 2. Cut the release (add --draft to stage it).
build/release.sh            # or: build/release.sh --draft
```

**Which repo does it release to?** `release.sh` calls `gh release create/upload`,
and `gh` resolves the target repo from the git `origin` remote. So the release
lands wherever `origin` points. To release to the fork, make sure `origin` is the
fork:

```sh
git remote set-url origin git@github.com:Symbiosis-Lab/onionpress.git
# (or push this branch to the fork so origin resolves there)
```

No change to `release.sh` is needed — it is remote-agnostic by design.

The demo target URL the moss side pins (roadmap "Release manifest"):

- `dmg_url`: `https://github.com/Symbiosis-Lab/onionpress/releases/latest/download/onionpress.dmg`
- `sha256`:  the value from `shasum -a 256 build/onionpress.dmg` above
- `version`: the fork release tag (e.g. `v2.4.107`)

## Notes / gotchas

- **arm64-only is the locked demo target.** The uv (single-arch) Python path
  produces an arm64-only MenubarApp — exactly right for the demo. Use python.org
  universal2 only if the DMG must also run on Intel.
- **py2app vs setuptools 81+**: the build auto-falls-back to `setuptools<81` if
  py2app trips over the removed `distutils.spawn(dry_run=…)`. No action needed.
- **mkp224o is mandatory.** A missing mkp224o aborts the DMG (never warns) — a
  vanity-less build would silently hand every install a random `.onion` instead
  of an `op2…` address.
- **`OnionPress.app/` is gitignored** — it is assembled at build time; never
  commit it.
