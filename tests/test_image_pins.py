#!/usr/bin/env python3
"""Which tor image do we pull, and which one do we run?

This fork publishes its own tor image because upstream's carries neither
obfs4proxy nor snowflake-client, so on a censored network a container started
from upstream's build is handed TOR_BRIDGE_LINES it has no binary to execute
— and it starts cleanly, which is why nothing surfaced it. Pinning the fork's
image is therefore only half the job: every site that pulls, probes or runs a
tor image has to name the same one, and build/refresh-image-digests.sh has to
keep all of them current at release time.

The script is exercised for real (in a throwaway repo, against a stub
`docker`) because its failure mode was silence: its matcher named upstream's
registry only, so once the fork pinned its own image the script rewrote the
refs that no longer mattered and skipped the ones that did, exit 0 either way.
"""

import ast
import importlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
REFRESH_SCRIPT = os.path.join(PROJECT_ROOT, "build", "refresh-image-digests.sh")

# Files build/refresh-image-digests.sh maintains. Kept here as well so a new
# pin site added to the script without a matching invariant — or an invariant
# without the script — shows up as a failure rather than as a stale pin.
MANAGED_FILES = [
    "app/Resources/docker/docker-compose.yml",
    "app/MacOS/onionpress",
    "app/Resources/docker/tor/onionheaven_common.py",
    "linux/onionpress",
    "src/onionpress/containers.py",
    "src/onionpress/launcher_ops.py",
]

# Same shape as the script's own matcher: a ref only counts when it sits in a
# pin context — a quoted string, a `${...:-...}` default, or end of line.
PIN_CONTEXT_REF = re.compile(
    r'ghcr\.io/(?P<org>[A-Za-z0-9._-]+)/(?P<image>onionpress-[A-Za-z0-9._-]+)'
    r':latest(?P<seam>"\s*\n\s*")?(?P<digest>@sha256:[a-f0-9]+)?(?=["}\n])')

NEW_TOR = "a" * 64          # digest the stub docker reports for the fork's tor
NEW_UPSTREAM_TOR = "b" * 64  # ... for upstream's tor
NEW_WP = "c" * 64            # ... for upstream's wordpress
OLD = "d" * 64               # whatever the tree was pinned to before


def _read(rel_path):
    with open(os.path.join(PROJECT_ROOT, rel_path), "r", encoding="utf-8") as f:
        return f.read()


class TestRefreshDigestsMaintainsBothRegistries(unittest.TestCase):
    """build/refresh-image-digests.sh, run for real against a stub `docker`.

    Two registries are in play and they are not interchangeable — the tor
    image is the fork's, the wordpress image is still upstream's. A ref must
    be re-pinned with the digest of the image it names.
    """

    # Representative of each pin shape in the tree. Content is trimmed to the
    # lines that matter; the script only cares about the refs.
    FIXTURES = {
        "app/Resources/docker/docker-compose.yml": (
            "services:\n"
            "  tor:\n"
            "    image: ${ONIONPRESS_TOR_IMAGE:-ghcr.io/symbiosis-lab/"
            f"onionpress-tor:latest@sha256:{OLD}}}\n"
            "  wordpress:\n"
            "    image: ghcr.io/brewsterkahle/onionpress-wordpress:latest"
            f"@sha256:{OLD}\n"
            "  onionheaven:\n"
            "    image: ${ONIONPRESS_TOR_IMAGE:-${ONIONHEAVEN_IMAGE:-"
            f"ghcr.io/symbiosis-lab/onionpress-tor:latest@sha256:{OLD}}}}}\n"
        ),
        # Unpinned upstream refs in a bash array: the matcher has to tolerate
        # a missing @sha256 so the script works on a fresh checkout too.
        "app/MacOS/onionpress": (
            '    local images=("ghcr.io/brewsterkahle/onionpress-tor:latest" '
            '"ghcr.io/brewsterkahle/onionpress-wordpress:latest" '
            '"mariadb:latest")\n'
        ),
        # Python string-concatenation seam — the ref is split across two
        # literals to keep the line readable.
        "app/Resources/docker/tor/onionheaven_common.py": (
            'TOR_IMAGE_DEFAULT = ("ghcr.io/symbiosis-lab/onionpress-tor:latest"\n'
            f'                     "@sha256:{OLD}")\n'
        ),
        # A shell default plus a bare presence check that must stay unpinned:
        # `docker image inspect` honours an @sha256 suffix, so pinning that
        # line would turn a "have we got the image at all?" probe into a
        # digest equality test.
        "linux/onionpress": (
            'export ONIONPRESS_TOR_IMAGE="${ONIONPRESS_TOR_IMAGE:-'
            f'ghcr.io/symbiosis-lab/onionpress-tor:latest@sha256:{OLD}}}"\n'
            "if docker image inspect ghcr.io/brewsterkahle/onionpress-tor:latest "
            ">/dev/null 2>&1; then\n"
            "    :\n"
            "fi\n"
        ),
        "src/onionpress/containers.py": (
            "ONIONHEAVEN_IMAGE = (\n"
            '    "ghcr.io/symbiosis-lab/onionpress-tor:latest"\n'
            f'    "@sha256:{OLD}"\n'
            ")\n"
        ),
        "src/onionpress/launcher_ops.py": (
            'DEFAULT_TOR_IMAGE = "ghcr.io/symbiosis-lab/onionpress-tor:latest'
            f'@sha256:{OLD}"\n'
        ),
    }

    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="onionpress-digests-test-")
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        os.makedirs(os.path.join(self.repo, "build"))
        self.script = os.path.join(self.repo, "build", "refresh-image-digests.sh")
        shutil.copy2(REFRESH_SCRIPT, self.script)
        for rel, body in self.FIXTURES.items():
            self._write(rel, body)
        # Stub `docker` first on PATH. Nothing here reaches a registry.
        self.bin = os.path.join(self.repo, "stub-bin")
        os.makedirs(self.bin)
        self._stub_docker({
            "ghcr.io/symbiosis-lab/onionpress-tor:latest": f"sha256:{NEW_TOR}",
            "ghcr.io/brewsterkahle/onionpress-tor:latest":
                f"sha256:{NEW_UPSTREAM_TOR}",
            "ghcr.io/brewsterkahle/onionpress-wordpress:latest": f"sha256:{NEW_WP}",
        })

    def _write(self, rel, body):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(body)

    def _read_out(self, rel):
        with open(os.path.join(self.repo, rel)) as f:
            return f.read()

    def _stub_docker(self, digests):
        """Answer `docker images --digests --format ...` and nothing else."""
        lines = "".join(
            f'    echo "{img} {digest}"\n' for img, digest in digests.items())
        path = os.path.join(self.bin, "docker")
        with open(path, "w") as f:
            f.write('#!/bin/sh\nif [ "$1" = "images" ]; then\n' + lines
                    + 'fi\nexit 0\n')
        os.chmod(path, 0o755)

    def _run(self):
        env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ["PATH"])
        return subprocess.run([shutil.which("bash") or "bash", self.script],
                              capture_output=True, text=True, env=env)

    def _module_constant(self, rel, name):
        """Evaluate a rewritten module-level string constant."""
        namespace = {}
        exec(compile(self._read_out(rel), rel, "exec"), namespace)
        return namespace[name]

    def test_each_ref_is_repinned_under_its_own_registry(self):
        """The bug: one matcher, one registry, and the other's refs skipped."""
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)

        compose = self._read_out("app/Resources/docker/docker-compose.yml")
        self.assertEqual(
            compose.count(
                f"ghcr.io/symbiosis-lab/onionpress-tor:latest@sha256:{NEW_TOR}"),
            2, "both the tor and onionheaven services take the fork's digest")
        self.assertIn(
            f"ghcr.io/brewsterkahle/onionpress-wordpress:latest@sha256:{NEW_WP}",
            compose)

        mac = self._read_out("app/MacOS/onionpress")
        self.assertIn(
            f"ghcr.io/brewsterkahle/onionpress-tor:latest@sha256:{NEW_UPSTREAM_TOR}",
            mac, "an upstream ref must get upstream's digest, not the fork's")
        self.assertNotIn(NEW_TOR, mac, "registries must not be collapsed")

        self.assertNotIn(OLD, compose + mac)

    def test_bare_presence_check_stays_unpinned(self):
        """`docker image inspect ghcr.io/...:latest` is a "have we got it?"
        probe. Pinning it would silently narrow it to one exact build."""
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(
            "if docker image inspect ghcr.io/brewsterkahle/onionpress-tor:latest "
            ">/dev/null 2>&1; then",
            self._read_out("linux/onionpress"))

    def test_split_string_ref_is_repinned_once(self):
        """A ref broken across two python literals must not end up carrying
        the new digest and the old one (`...@sha256:new@sha256:old`)."""
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        expected = f"ghcr.io/symbiosis-lab/onionpress-tor:latest@sha256:{NEW_TOR}"
        self.assertEqual(
            self._module_constant(
                "app/Resources/docker/tor/onionheaven_common.py",
                "TOR_IMAGE_DEFAULT"),
            expected)
        self.assertEqual(
            self._module_constant("src/onionpress/containers.py",
                                  "ONIONHEAVEN_IMAGE"),
            expected)
        self.assertEqual(
            self._module_constant("src/onionpress/launcher_ops.py",
                                  "DEFAULT_TOR_IMAGE"),
            expected)

    def test_unreferenced_image_is_not_looked_up(self):
        """Dropping an image from the tree must not oblige a release to pull
        it. The fork no longer runs upstream's tor image on Linux."""
        self._write("app/MacOS/onionpress",
                    '    local images=("ghcr.io/brewsterkahle/'
                    'onionpress-wordpress:latest" "mariadb:latest")\n')
        self._stub_docker({
            "ghcr.io/symbiosis-lab/onionpress-tor:latest": f"sha256:{NEW_TOR}",
            "ghcr.io/brewsterkahle/onionpress-wordpress:latest": f"sha256:{NEW_WP}",
        })
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("not referenced, skipping: "
                      "ghcr.io/brewsterkahle/onionpress-tor:latest", proc.stdout)

    def test_unknown_registry_is_an_error_not_a_silent_skip(self):
        """The whole point: a ref this script can't maintain must say so."""
        self._write("src/onionpress/launcher_ops.py",
                    'DEFAULT_TOR_IMAGE = "ghcr.io/someone-else/'
                    'onionpress-tor:latest"\n')
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0,
                            "an unmaintained pin must fail the release step")
        self.assertIn("src/onionpress/launcher_ops.py", proc.stderr)
        self.assertIn("someone-else", proc.stderr)

    def test_missing_local_image_is_an_error(self):
        """Better to noise out than write an empty or wrong digest."""
        self._stub_docker({
            "ghcr.io/symbiosis-lab/onionpress-tor:latest": "<none>",
            "ghcr.io/brewsterkahle/onionpress-tor:latest":
                f"sha256:{NEW_UPSTREAM_TOR}",
            "ghcr.io/brewsterkahle/onionpress-wordpress:latest": f"sha256:{NEW_WP}",
        })
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("docker pull ghcr.io/symbiosis-lab/onionpress-tor:latest",
                      proc.stderr)
        self.assertIn(OLD, self._read_out("app/Resources/docker/docker-compose.yml"))


class TestEveryTorPinNamesTheForksImage(unittest.TestCase):
    """Cross-file invariant over the tree itself.

    Fork PR #2 pinned compose's `tor` and `onionheaven` services and left
    every other site on upstream's image, which is how a stack could pull one
    tor image and run another.
    """

    def _refs(self, rel_path):
        return list(PIN_CONTEXT_REF.finditer(_read(rel_path)))

    def test_the_invariant_covers_every_file_the_script_rewrites(self):
        """A pin site added to the script but not here would go unchecked;
        one added here but not there would never be refreshed."""
        block = re.search(r"^FILES = \[(.*?)^\]", _read("build/refresh-image-digests.sh"),
                          re.DOTALL | re.MULTILINE)
        self.assertIsNotNone(block, "could not find FILES in the refresh script")
        self.assertEqual(sorted(re.findall(r'"([^"]+)"', block.group(1))),
                         sorted(MANAGED_FILES))

    def test_canonical_pin_comes_from_compose(self):
        """docker-compose.yml is what starts the containers, so its default is
        the pin every other site has to agree with."""
        refs = [m for m in self._refs("app/Resources/docker/docker-compose.yml")
                if m.group("image") == "onionpress-tor"]
        self.assertTrue(refs, "compose must pin a tor image")
        for match in refs:
            self.assertEqual(match.group("org"), "symbiosis-lab")
            self.assertTrue(match.group("digest"))

    def test_every_managed_file_agrees_with_compose(self):
        compose_refs = [
            m for m in self._refs("app/Resources/docker/docker-compose.yml")
            if m.group("image") == "onionpress-tor"]
        canonical = compose_refs[0].group("digest")
        for rel_path in MANAGED_FILES:
            for match in self._refs(rel_path):
                line = _read(rel_path).count("\n", 0, match.start()) + 1
                where = f"{rel_path}:{line}"
                self.assertTrue(
                    match.group("digest"),
                    f"{where}: pinned-context ref without a digest — "
                    f"build/refresh-image-digests.sh maintains this file, so "
                    f"an unpinned ref here ships whatever :latest resolves to")
                if match.group("image") != "onionpress-tor":
                    continue
                self.assertEqual(
                    match.group("org"), "symbiosis-lab",
                    f"{where}: upstream's tor image ships neither obfs4proxy "
                    f"nor snowflake-client — this site must run the fork's "
                    f"build like compose does")
                self.assertEqual(
                    match.group("digest"), canonical,
                    f"{where}: digest has drifted from docker-compose.yml; "
                    f"run build/refresh-image-digests.sh")


class TestLauncherOpsDefaultImage(unittest.TestCase):
    """DEFAULT_TOR_IMAGE is what mkp224o is run out of.

    `tor_image_has_mkp224o()` inspects it and returns False when it isn't
    present locally — and the only tor image the stack pulls is compose's
    default. Naming upstream's build there meant first-run vanity generation
    was skipped on every fresh Linux install and the user silently got a
    random address instead of their prefix.
    """

    def _reload(self):
        import onionpress.launcher_ops as launcher_ops
        return importlib.reload(launcher_ops)

    def tearDown(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ONIONPRESS_TOR_IMAGE", None)
            os.environ.pop("ONIONHEAVEN_IMAGE", None)
            self._reload()

    def test_default_is_the_forks_pinned_image(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            launcher_ops = self._reload()
        self.assertIn("ghcr.io/symbiosis-lab/onionpress-tor:latest",
                      launcher_ops.DEFAULT_TOR_IMAGE)
        self.assertIn("@sha256:", launcher_ops.DEFAULT_TOR_IMAGE)

    def test_env_override_is_honoured(self):
        """The Linux launcher exports ONIONPRESS_TOR_IMAGE before running
        `python3 -m onionpress.cli generate-vanity`, so a locally built image
        gets probed instead of the pin the launcher isn't using."""
        with mock.patch.dict(os.environ,
                             {"ONIONPRESS_TOR_IMAGE": "onionpress-tor:local"}):
            launcher_ops = self._reload()
            self.assertEqual(launcher_ops.DEFAULT_TOR_IMAGE,
                             "onionpress-tor:local")

    def test_older_variable_name_still_works(self):
        env = {"ONIONHEAVEN_IMAGE": "onionpress-tor:legacy"}
        with mock.patch.dict(os.environ, env, clear=True):
            launcher_ops = self._reload()
            self.assertEqual(launcher_ops.DEFAULT_TOR_IMAGE,
                             "onionpress-tor:legacy")

    def test_vanity_helpers_default_to_it(self):
        """Both helpers must take the constant, not a literal of their own."""
        tree = ast.parse(_read("src/onionpress/launcher_ops.py"))
        for name in ("tor_image_has_mkp224o", "generate_vanity_in_container"):
            func = next(n for n in tree.body
                        if isinstance(n, ast.FunctionDef) and n.name == name)
            defaults = [d for d in func.args.defaults + func.args.kw_defaults
                        if d is not None]
            self.assertIn(
                "DEFAULT_TOR_IMAGE",
                [ast.unparse(d) for d in defaults],
                f"{name}() must default to DEFAULT_TOR_IMAGE")


class TestLinuxLauncherPullsWhatItRuns(unittest.TestCase):
    """The Linux stack is started by `docker compose up`, so compose's default
    governs and every other mention of the image in the launcher has to derive
    from it. Before, `update_images` pre-pulled a hardcoded upstream ref: a
    UPDATE_ON_LAUNCH=yes install spent the bandwidth on an image it never ran.
    """

    def setUp(self):
        self.script = _read("linux/onionpress")

    def test_image_is_defined_once(self):
        literals = re.findall(r"ghcr\.io/[A-Za-z0-9._-]+/onionpress-tor:latest",
                              self.script)
        self.assertEqual(
            len(literals), 1,
            "the tor image belongs in exactly one place in this file "
            "(ONIONPRESS_TOR_IMAGE); every other use must read the variable")

    def test_exported_before_compose_runs(self):
        """compose only interpolates ${ONIONPRESS_TOR_IMAGE} from the
        environment, so the export has to precede the first compose call."""
        export = re.search(r'^export ONIONPRESS_TOR_IMAGE=', self.script,
                           re.MULTILINE)
        self.assertIsNotNone(export, "ONIONPRESS_TOR_IMAGE must be exported")
        # A command, not a mention of one in a comment.
        first_compose = re.search(r'^\s*docker compose ', self.script,
                                  re.MULTILINE)
        self.assertIsNotNone(first_compose)
        self.assertLess(export.start(), first_compose.start())

    def test_prepull_list_uses_the_variable(self):
        images = re.search(r"local images=\((.*?)\)", self.script, re.DOTALL)
        self.assertIsNotNone(images, "update_images must keep its pull list")
        self.assertIn('"$ONIONPRESS_TOR_IMAGE"', images.group(1))

    def test_mkp224o_probe_uses_the_variable(self):
        self.assertIn('docker image inspect "$ONIONPRESS_TOR_IMAGE"',
                      self.script)

    def test_override_falls_back_to_the_older_variable_name(self):
        self.assertIn("${ONIONPRESS_TOR_IMAGE:-${ONIONHEAVEN_IMAGE:-",
                      self.script)


if __name__ == "__main__":
    unittest.main()
