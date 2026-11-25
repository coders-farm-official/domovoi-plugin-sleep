'''The plugin against the REAL cores the houses run: Domovoi 1.4.0 (the
oldest supported, plugin SDK 1.11), core main, and next/1.5.0 (SDK 1.18
today). Two houses follow main, so a plugin must keep working there.

Each case runs this plugin's own manifest, loader and handler tests in a
subprocess whose import path puts that core first, so what that core's
loader accepts and its SDK provides is what is tested (the pattern of the
weather plugin). The cores come from git: ``DOMOVOI_CORE_REPO`` (a
Domovoi checkout; default the sibling ``../domovoi``) supplies the
v1.4.0 tag, ``origin/main`` (``DOMOVOI_CORE_MAIN_REF``) and
``origin/next/1.5.0`` (``DOMOVOI_CORE_NEXT_REF``); fetch it first. A ref
the checkout lacks is skipped.
'''

from __future__ import annotations

import os
import re
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path

import pytest

from tests.conftest import PLUGIN_ROOT

CORE_REPO = Path(os.environ.get("DOMOVOI_CORE_REPO", PLUGIN_ROOT.parent / "domovoi"))
MAIN_REF = os.environ.get("DOMOVOI_CORE_MAIN_REF", "origin/main")
NEXT_REF = os.environ.get("DOMOVOI_CORE_NEXT_REF", "origin/next/1.5.0")
SUITE = ["tests/test_manifest.py", "tests/test_plugin_load.py", "tests/test_handler.py", "tests/test_route_tiers.py"]

PROBE = r"""
import sys
from pathlib import Path
import domovoi.sdk as sdk
from domovoi.plugins_runtime.manifest import parse_manifest_dir
parse_manifest_dir(Path(sys.argv[1]))
print("SDK", sdk.API_VERSION)
"""


_SITECUSTOMIZE = (
    "import sys\n"
    "sys.meta_path[:] = [f for f in sys.meta_path\n"
    "                    if 'editable' not in type(f).__name__.lower()\n"
    "                    and 'editable' not in getattr(f, '__name__', '').lower()\n"
    "                    and '__editable__' not in getattr(f, '__module__', '')]\n"
)
_NO_EDITABLE: list[Path] = []


def _no_editable_dir() -> Path:
    """A folder whose ``sitecustomize`` drops an editable install's import
    finder: in a development venv (``pip install -e`` of a Domovoi
    checkout) that finder serves any ``domovoi.*`` module the archived
    core lacks from the checkout instead, so an old core would quietly
    gain newer modules (a real v1.4.0 house has no ``domovoi.home_location``)."""
    if not _NO_EDITABLE:
        import tempfile

        d = Path(tempfile.mkdtemp(prefix="no-editable-"))
        (d / "sitecustomize.py").write_text(_SITECUSTOMIZE, encoding="utf-8")
        _NO_EDITABLE.append(d)
    return _NO_EDITABLE[0]


def _run(core: Path, args: list[str], home: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(_no_editable_dir()), str(core)]), "PYTHONUTF8": "1",
           "DOMOVOI_TEST_HOME_GUARD": "off", "HOME": str(home), "USERPROFILE": str(home)}
    return subprocess.run([sys.executable, *args], cwd=PLUGIN_ROOT, env=env,
                          capture_output=True, text=True, timeout=600)


def _core_at(ref: str, tmp_path_factory) -> Path:
    if not (CORE_REPO / ".git").exists():
        pytest.skip(f"no Domovoi checkout to take {ref} from (DOMOVOI_CORE_REPO)")
    found = subprocess.run(["git", "-C", str(CORE_REPO), "rev-parse", "--verify", "-q",
                            f"{ref}^{{commit}}"], capture_output=True, text=True)
    if found.returncode != 0:
        pytest.skip(f"{ref} not in the Domovoi checkout")
    dest = tmp_path_factory.mktemp("core")
    tar = dest / "core.tar"
    # The package and the core's lock (a plugin's pins are checked against it).
    subprocess.run(["git", "-C", str(CORE_REPO), "archive", "-o", str(tar), ref,
                    "domovoi", "requirements.lock"], check=True)
    with tarfile.open(tar) as t:
        t.extractall(dest, filter="data")
    return dest


def _accepts(api: str, version: str) -> bool:
    """Does the manifest's ``domovoi_api`` range (">=a.b,<c.d") take
    this SDK version?"""
    v = tuple(int(x) for x in version.split(".")[:2])
    for part in api.split(","):
        m = re.fullmatch(r"\s*(>=|<|<=|>|==)\s*(\d+)\.(\d+)\s*", part)
        assert m, f"unexpected domovoi_api part {part!r}"
        b = (int(m.group(2)), int(m.group(3)))
        ok = {">=": v >= b, "<": v < b, "<=": v <= b, ">": v > b, "==": v == b}[m.group(1)]
        if not ok:
            return False
    return True


def _suite(core: Path, tmp_path: Path) -> str:
    probe = _run(core, ["-c", PROBE, str(PLUGIN_ROOT)], tmp_path)
    assert probe.returncode == 0, probe.stderr[-2000:]
    version = probe.stdout.split("SDK", 1)[1].split()[0]
    manifest = tomllib.loads((PLUGIN_ROOT / "domovoi-plugin.toml").read_text(encoding="utf-8"))
    assert _accepts(manifest["plugin"]["domovoi_api"], version), (
        f"domovoi_api {manifest['plugin']['domovoi_api']!r} refuses SDK {version}")
    r = _run(core, ["-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider",
                    "--rootdir", str(PLUGIN_ROOT), *SUITE], tmp_path)
    assert r.returncode == 0, (r.stdout + r.stderr)[-4000:]
    return probe.stdout + r.stdout


@pytest.mark.parametrize("ref", ["v1.4.0", MAIN_REF, NEXT_REF])
def test_installs_loads_and_answers_on_each_core(ref, tmp_path_factory, tmp_path) -> None:
    out = _suite(_core_at(ref, tmp_path_factory), tmp_path)
    if ref == "v1.4.0":
        assert "SDK 1.11.0" in out      # really the old core, not the venv's
