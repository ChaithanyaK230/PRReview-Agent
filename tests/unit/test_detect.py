from core.analysis.config import RepoConfig
from core.analysis.detect import UV, detect

ROOT = "/home/user/work/head"
PY = f"{ROOT}/.venv/bin/python"


def plan(*files, **config):
    return detect(set(files), ROOT, RepoConfig(**config))


def test_uv_lock_uses_frozen_sync():
    tc = plan("uv.lock", "pyproject.toml")
    assert tc.strategy == "uv"
    assert tc.install_steps[0] == [UV, "sync", "--frozen", "--project", ROOT]


def test_requirements_files_all_installed_in_order():
    tc = plan("requirements.txt", "requirements-dev.txt", "app.py")
    assert tc.strategy == "requirements"
    assert tc.install_steps[1] == [UV, "pip", "install", "--python", PY,
                                   "-r", "requirements-dev.txt", "-r", "requirements.txt", "pytest"]


def test_pyproject_without_lock_installs_editable():
    tc = plan("pyproject.toml")
    assert tc.strategy == "pyproject" and "-e" in tc.install_steps[1]


def test_bare_python_files_still_get_pytest():
    tc = plan("calculator.py", "test_calculator.py")
    assert (tc.language, tc.strategy) == ("python", "none")
    assert tc.install_steps[-1][-1] == "pytest"


def test_js_only_repo_is_skipped():
    tc = plan("package.json", "index.ts")
    assert not tc.supported and tc.install_steps == []


def test_config_install_overrides_detection():
    tc = plan("uv.lock", install=["pip install -e .[dev]"])
    assert tc.strategy == "custom"
    assert tc.install_steps == [["bash", "-lc", "pip install -e .[dev]"]]
