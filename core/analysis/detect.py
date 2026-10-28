from dataclasses import dataclass, field

from core.analysis.config import RepoConfig
from core.sandbox.base import WORK_ROOT

# Shared tools venv, installed once per sandbox. ruff lives here so base and head are
# linted by the SAME ruff version: a version difference would look like "new" findings.
TOOLS_DIR = f"{WORK_ROOT}/tools"
UV = f"{TOOLS_DIR}/bin/uv"
TOOL_PACKAGES = ["uv", "ruff"]
TEST_PACKAGES = ["pytest"]

PYTHON_SIGNALS = ("uv.lock", "requirements.txt", "pyproject.toml", "setup.py", "setup.cfg")
JS_SIGNALS = ("package.json", "pnpm-lock.yaml", "package-lock.json")


@dataclass(frozen=True)
class Toolchain:
    language: str  # "python" | "unsupported"
    strategy: str  # how deps are installed: uv | requirements | pyproject | none | custom | skipped
    install_steps: list[list[str]] = field(default_factory=list)

    @property
    def supported(self) -> bool:
        return self.language == "python"


def venv_python(root: str) -> str:
    return f"{root}/.venv/bin/python"


def detect(root_files: set[str], root: str, config: RepoConfig) -> Toolchain:
    """Pick how to install one side of the repo from the files in its root (LLD §10.2, Python only for now)."""
    has_python = any(f in root_files for f in PYTHON_SIGNALS) or any(f.endswith(".py") for f in root_files)
    if config.language == "typescript" or (not has_python and any(f in root_files for f in JS_SIGNALS)):
        return Toolchain("unsupported", "skipped")  # TS/JS runners come later; review stays diff-only

    py = venv_python(root)
    if config.install:
        # Maintainer-provided commands from the BASE branch config; still run inside the sandbox
        return Toolchain("python", "custom", [["bash", "-lc", cmd] for cmd in config.install])
    if "uv.lock" in root_files:
        # uv sync also fetches the Python version the project pins, if the sandbox lacks it
        return Toolchain("python", "uv", [
            [UV, "sync", "--frozen", "--project", root],
            [UV, "pip", "install", "--python", py, *TEST_PACKAGES],
        ])

    create_venv = [UV, "venv", f"{root}/.venv"]
    requirements = sorted(f for f in root_files if f.startswith("requirements") and f.endswith(".txt"))
    if requirements:
        args = [a for r in requirements for a in ("-r", r)]
        return Toolchain("python", "requirements", [
            create_venv, [UV, "pip", "install", "--python", py, *args, *TEST_PACKAGES],
        ])
    if "pyproject.toml" in root_files or "setup.py" in root_files:
        return Toolchain("python", "pyproject", [
            create_venv, [UV, "pip", "install", "--python", py, "-e", root, *TEST_PACKAGES],
        ])
    return Toolchain("python", "none", [create_venv, [UV, "pip", "install", "--python", py, *TEST_PACKAGES]])
