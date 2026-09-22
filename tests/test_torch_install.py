"""How bootstrap installs PyTorch: uv's `--torch-backend`, a smoke test, and the rules
that keep a GPU upgrade from ever costing a working venv.

No network, no GPU, no real pip: `subprocess.run` is replaced by a fake that answers
each command the way pip, uv and the venv's interpreter would.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from hireshire import paths

sys.path.insert(0, str(paths.ROOT / "scripts"))

import bootstrap  # noqa: E402


class FakeRun:
    """Answers bootstrap's subprocess calls from a scenario, and records them."""

    def __init__(self, *, build="cpu", uv_rc=0, uv_pip_rc=0, smoke=None, cpu_smoke=None):
        self.build = build            # what `_installed_torch` reports: gpu | cpu | None
        self.uv_rc = uv_rc            # `uv pip install` exit code
        self.uv_pip_rc = uv_pip_rc    # `pip install --upgrade uv` exit code
        self.smoke = smoke or "ok\t2.14.0+cu130\tNVIDIA GeForce RTX 3060 Laptop GPU"
        self.cpu_smoke = cpu_smoke or "ok\t2.14.0+cpu\t"
        self.calls: list[list[str]] = []
        self._backend = None

    def uv_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c[1:5] == ["-m", "uv", "pip", "install"]]

    def __call__(self, cmd, **kwargs):
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        if cmd[1:3] == ["-m", "pip"]:
            return subprocess.CompletedProcess(cmd, self.uv_pip_rc, "", "pip failed")
        if cmd[1:5] == ["-m", "uv", "pip", "install"]:
            self._backend = cmd[cmd.index("--torch-backend") + 1]
            return subprocess.CompletedProcess(cmd, self.uv_rc, "", "network is down")
        if cmd[1] == "-c" and cmd[2] == bootstrap._BUILD_SCRIPT:
            if self.build is None:
                return subprocess.CompletedProcess(cmd, 1, "", "No module named torch")
            return subprocess.CompletedProcess(cmd, 0, self.build + "\n", "")
        if cmd[1] == "-c" and cmd[2] == bootstrap._SMOKE_SCRIPT:
            line = self.cpu_smoke if self._backend == "cpu" else self.smoke
            return subprocess.CompletedProcess(cmd, 0, line + "\n", "")
        raise AssertionError(f"unexpected command {cmd}")


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A machine with an existing venv, no sweep running, on Windows, with a GPU tool."""
    data = tmp_path / "data"
    data.mkdir()
    py = data / "venv" / "Scripts" / "python.exe"
    py.parent.mkdir(parents=True)
    py.write_text("")
    req = tmp_path / "requirements-core.txt"
    req.write_bytes(b"sentence-transformers>=5.0\n")

    monkeypatch.setattr(bootstrap, "DATA", data)
    monkeypatch.setattr(bootstrap, "LOCK", data / "requirements.lock")
    monkeypatch.setattr(bootstrap, "TORCH_VARIANT", data / "torch_variant")
    monkeypatch.setattr(bootstrap, "REQUIREMENTS", req)
    monkeypatch.setattr(bootstrap, "venv_python", lambda *a: py)
    monkeypatch.setattr(bootstrap, "rescue_stranded_data", lambda: None)
    monkeypatch.setattr(bootstrap.sweep_pid, "read", lambda _d=None: None)
    monkeypatch.setattr(bootstrap.venv, "EnvBuilder", lambda *a, **k: pytest.fail("venv rebuilt"))
    monkeypatch.setattr(bootstrap.sys, "platform", "win32")
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: f"C:/Windows/{name}.exe")
    monkeypatch.delenv("HIRESHIRE_TORCH", raising=False)
    return bootstrap


def _install(monkeypatch, fake: FakeRun) -> FakeRun:
    monkeypatch.setattr(bootstrap.subprocess, "run", fake)
    return fake


# --- which backend is asked for ---------------------------------------------------


def test_the_default_request_is_auto(env):
    assert env._torch_backend() == "auto"


def test_the_support_override_wins(env, monkeypatch):
    monkeypatch.setenv("HIRESHIRE_TORCH", "cu126")
    assert env._torch_backend() == "cu126"


def test_an_architecture_fallback_pins_cpu(env):
    env.TORCH_VARIANT.write_text("cpu (architecture fallback: no kernel image)\n")
    assert env._torch_backend() == "cpu"


def test_an_ordinary_variant_record_does_not_pin_anything(env):
    env.TORCH_VARIANT.write_text("2.14.0+cu130 NVIDIA GeForce RTX 3060\n")
    assert env._torch_backend() == "auto"


def test_macos_skips_uv_entirely(env, monkeypatch):
    monkeypatch.setattr(bootstrap.sys, "platform", "darwin")
    fake = _install(monkeypatch, FakeRun())
    assert env._torch_backend() is None
    assert env.main() == 0
    assert fake.uv_calls() == []
    assert env.LOCK.read_bytes().endswith(b"# torch-backend: pypi\n")


# --- the lock -----------------------------------------------------------------------


def test_a_pre_uv_lock_is_not_current(env):
    """Every existing install must upgrade once, through the bootstrap calls that
    run_engine.py and run_orchestration.py already make."""
    env.LOCK.write_bytes(env.REQUIREMENTS.read_bytes())
    assert env.is_current() is False


def test_the_lock_records_the_request(env, monkeypatch):
    _install(monkeypatch, FakeRun())
    assert env.main() == 0
    assert env.LOCK.read_bytes().endswith(b"# torch-backend: auto\n")
    assert env.is_current() is True


# --- replacing a CPU build ------------------------------------------------------------


def test_a_cpu_build_is_replaced_when_a_gpu_tool_exists(env, monkeypatch):
    fake = _install(monkeypatch, FakeRun(build="cpu"))
    assert env.main() == 0
    (call,) = fake.uv_calls()
    assert "--reinstall-package" in call
    assert call[call.index("--torch-backend") + 1] == "auto"


def test_no_forced_reinstall_without_a_gpu_tool(env, monkeypatch):
    """Otherwise every GPU-less install re-downloads the CPU build once."""
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: None)
    fake = _install(monkeypatch, FakeRun(build="cpu", smoke="ok\t2.14.0+cpu\t"))
    assert env.main() == 0
    assert "--reinstall-package" not in fake.uv_calls()[0]


def test_no_forced_reinstall_of_an_existing_gpu_build(env, monkeypatch):
    """Linux PyPI already ships a CUDA build; replacing it would be ~3 GB for nothing."""
    fake = _install(monkeypatch, FakeRun(build="gpu"))
    assert env.main() == 0
    assert "--reinstall-package" not in fake.uv_calls()[0]


def test_no_forced_reinstall_when_cpu_is_requested(env, monkeypatch):
    monkeypatch.setenv("HIRESHIRE_TORCH", "cpu")
    fake = _install(monkeypatch, FakeRun(build="cpu"))
    assert env.main() == 0
    assert "--reinstall-package" not in fake.uv_calls()[0]


# --- the smoke test ---------------------------------------------------------------------


def test_bad_kernels_fall_back_to_cpu_and_pin_it(env, monkeypatch):
    fake = _install(monkeypatch, FakeRun(
        smoke="bad_kernels\t2.14.0+cu130\tCUDA error: no kernel image is available",
    ))
    assert env.main() == 0
    first, second = fake.uv_calls()
    assert second[second.index("--torch-backend") + 1] == "cpu"
    assert "--reinstall-package" in second
    assert env.TORCH_VARIANT.read_text().startswith(env._ARCH_FALLBACK)
    # The pin survives into the lock, so the next start does not retry the GPU build.
    assert env.LOCK.read_bytes().endswith(b"# torch-backend: cpu\n")
    assert env.is_current() is True


def test_an_unavailable_gpu_keeps_the_gpu_build(env, monkeypatch):
    """A transient fault must never pin the install to CPU for good."""
    fake = _install(monkeypatch, FakeRun(smoke="no_gpu\t2.14.0+cu130\t"))
    assert env.main() == 0
    assert len(fake.uv_calls()) == 1
    assert not env.TORCH_VARIANT.read_text().startswith(env._ARCH_FALLBACK)
    assert env.LOCK.read_bytes().endswith(b"# torch-backend: auto\n")


def test_the_variant_file_names_the_build(env, monkeypatch):
    _install(monkeypatch, FakeRun())
    assert env.main() == 0
    assert env.TORCH_VARIANT.read_text().startswith("2.14.0+cu130 NVIDIA")


# --- failures ----------------------------------------------------------------------------


def test_a_failed_upgrade_keeps_sweeping_on_the_old_build(env, monkeypatch):
    """Only the torch line changed and torch still imports: return 0, write no lock."""
    env.LOCK.write_bytes(env.REQUIREMENTS.read_bytes())  # pre-uv lock, same requirements
    _install(monkeypatch, FakeRun(uv_rc=1))
    assert env.main() == 0
    assert env.LOCK.read_bytes() == env.REQUIREMENTS.read_bytes()
    assert env.is_current() is False  # so the next start retries


def test_a_failed_uv_bootstrap_is_the_same_soft_failure(env, monkeypatch):
    env.LOCK.write_bytes(env.REQUIREMENTS.read_bytes())
    _install(monkeypatch, FakeRun(uv_pip_rc=1))
    assert env.main() == 0


def test_a_failed_install_with_new_requirements_is_an_error(env, monkeypatch):
    env.LOCK.write_bytes(b"sentence-transformers>=4.0\n")
    _install(monkeypatch, FakeRun(uv_rc=1))
    assert env.main() != 0
    assert not env.LOCK.read_bytes().endswith(b"auto\n")


def test_a_failed_upgrade_without_a_working_torch_is_an_error(env, monkeypatch):
    env.LOCK.write_bytes(env.REQUIREMENTS.read_bytes())
    _install(monkeypatch, FakeRun(uv_rc=1, build=None))
    assert env.main() != 0


def test_a_torch_that_does_not_import_after_install_is_a_failure(env, monkeypatch):
    fake = FakeRun()
    original = fake.__call__

    def broken_smoke(cmd, **kwargs):
        if [str(c) for c in cmd][1:3] == ["-c", bootstrap._SMOKE_SCRIPT]:
            return subprocess.CompletedProcess(cmd, 1, "", "ImportError: DLL load failed")
        return original(cmd, **kwargs)

    monkeypatch.setattr(bootstrap.subprocess, "run", broken_smoke)
    assert env.main() != 0
    assert not env.LOCK.exists()


# --- a live sweep --------------------------------------------------------------------------


def test_nothing_is_touched_while_a_sweep_is_running(env, monkeypatch):
    """bootstrap runs before the sweeper's own duplicate guard, and on Windows swapping
    torch under a live process half-removes it."""
    monkeypatch.setattr(bootstrap.sweep_pid, "read", lambda _d=None: 4321)
    monkeypatch.setattr(bootstrap, "is_alive", lambda pid: True)
    fake = _install(monkeypatch, FakeRun())
    assert env.main() == 0
    assert fake.calls == []
    assert not env.LOCK.exists()


def test_a_dead_recorded_sweep_does_not_block_the_upgrade(env, monkeypatch):
    monkeypatch.setattr(bootstrap.sweep_pid, "read", lambda _d=None: 4321)
    monkeypatch.setattr(bootstrap, "is_alive", lambda pid: False)
    fake = _install(monkeypatch, FakeRun())
    assert env.main() == 0
    assert fake.uv_calls()


# --- check() ---------------------------------------------------------------------------------


def test_check_says_update_not_gpu_for_a_stale_venv(env, monkeypatch, capsys):
    env.LOCK.write_bytes(env.REQUIREMENTS.read_bytes())
    monkeypatch.setattr(bootstrap.subprocess, "run", lambda *a, **k: pytest.fail("installed"))
    assert env.check() == 0
    out = capsys.readouterr().out
    assert "dependencies need updating" in out
    assert "GPU" not in out
