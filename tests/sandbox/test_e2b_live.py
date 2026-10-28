"""Real E2B sandbox tests. They cost a little money, so they only run with: uv run pytest -m sandbox"""

import io
import tarfile

import pytest

from core.sandbox.base import WORK_ROOT
from core.sandbox.e2b import E2BSandbox
from core.settings import get_settings

pytestmark = pytest.mark.sandbox

PYPI = ["curl", "-sS", "-m", "5", "-o", "/dev/null", "-w", "%{http_code}", "https://pypi.org"]


def github_style_tarball() -> bytes:
    """A .tar.gz with one top-level folder, like GitHub's /tarball endpoint returns."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        content = b"def add(a, b):\n    return a + b\n"
        info = tarfile.TarInfo("owner-repo-abc1234/calc.py")
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


@pytest.fixture
async def sbx():
    key = get_settings().e2b_api_key
    if key is None:
        pytest.skip("E2B_API_KEY not set")
    sandbox = await E2BSandbox.create(key.get_secret_value(), lifetime_s=300)
    yield sandbox
    await sandbox.close()


async def test_upload_exec_and_network_cutoff(sbx):
    dest = f"{WORK_ROOT}/head"
    await sbx.upload_tree(github_style_tarball(), dest, strip_components=1)

    result = await sbx.exec(["python3", "-c", "import calc; print(calc.add(2, 3))"], cwd=dest, timeout_s=30)
    assert result.ok and result.stdout.strip() == "5"

    failing = await sbx.exec(["python3", "-c", "raise SystemExit(3)"], cwd=dest, timeout_s=30)
    assert failing.exit_code == 3

    assert (await sbx.exec(PYPI, cwd="/", timeout_s=15)).stdout == "200"
    await sbx.set_network(False)
    assert not (await sbx.exec(PYPI, cwd="/", timeout_s=15)).ok


async def test_timeout_is_reported(sbx):
    result = await sbx.exec(["sleep", "30"], cwd="/", timeout_s=2)
    assert result.timed_out
