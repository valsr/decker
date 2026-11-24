import os
import subprocess
import sys
from pathlib import Path

import pytest
from assertpy import assert_that

from decker.plugin.environment import venv_python
from decker.plugin.manifest import RuntimeSpec
from decker.plugin.runtime.launcher import launch_command, launch_env


def test_python_uses_plugin_venv_uses_plugin_venv(tmp_path: Path) -> None:
    cmd = launch_command(tmp_path, RuntimeSpec("python", "main.py"))
    assert_that(cmd[0]).is_equal_to(str(venv_python(tmp_path)))
    assert_that(cmd[1:3]).is_equal_to(["-m", "decker_plugin_sdk"])


def test_builtin_python_runtime_uses_the_server_interpreter(tmp_path: Path) -> None:
    cmd = launch_command(tmp_path, RuntimeSpec("python", "main.py"), builtin=True)
    assert_that(cmd).is_equal_to([sys.executable, "-m", "decker_plugin_sdk", str(tmp_path / "main.py")])


def test_user_python_runtime_still_uses_the_venv(tmp_path: Path) -> None:
    cmd = launch_command(tmp_path, RuntimeSpec("python", "main.py"), builtin=False)
    assert_that(cmd[0]).is_equal_to(str(venv_python(tmp_path)))


def test_builtin_entry_outside_plugin_dir_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        launch_command(tmp_path, RuntimeSpec("python", "../evil.py"), builtin=True)


def test_executable_runs_entry_directly(tmp_path: Path) -> None:
    cmd = launch_command(tmp_path, RuntimeSpec("executable", "bin/run"))
    assert_that(cmd).is_equal_to([str(tmp_path / "bin/run")])


def test_entry_outside_plugin_dir_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        launch_command(tmp_path, RuntimeSpec("python", "../evil.py"))
    with pytest.raises(ValueError):
        launch_command(tmp_path, RuntimeSpec("executable", "/bin/sh"))


def test_env_is_minimal_and_has_sdk_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_TOKEN", "x")
    env = launch_env(tmp_path)
    assert_that(env).contains_key("PATH", "HOME", "LANG")
    assert_that(env).does_not_contain_key("SECRET_TOKEN")
    assert_that(env["PYTHONUNBUFFERED"]).is_equal_to("1")
    assert_that(env["PYTHONPATH"]).is_equal_to(str(tmp_path))
    assert_that(launch_env(None)).does_not_contain_key("PYTHONPATH")


def test_launch_argv_uses_the_interpreter_in_a_directory_with_spaces(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "my plugins" / "p"
    plugin_dir.mkdir(parents=True)
    cmd = launch_command(plugin_dir, RuntimeSpec("python", "main.py"))
    assert_that(cmd[0]).is_equal_to(str(venv_python(plugin_dir)))
    assert_that(cmd[-1]).is_equal_to(str(plugin_dir / "main.py"))


_WINDOWS_VARS = (
    "SYSTEMROOT",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "TEMP",
    "TMP",
    "APPDATA",
    "LOCALAPPDATA",
    "COMSPEC",
    "PATHEXT",
)


def test_windows_env_carries_system_variables_without_inventing_any(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = {"PATH": "C:\\bin", "SYSTEMROOT": "C:\\Windows", "TEMP": "C:\\t", "COMSPEC": "cmd.exe", "SECRET": "x"}
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "environ", fake)
    env = launch_env(None)
    assert_that(env).contains_entry({"SYSTEMROOT": "C:\\Windows"}, {"TEMP": "C:\\t"}, {"COMSPEC": "cmd.exe"})
    assert_that(env).does_not_contain_key("SECRET", "USERPROFILE", "APPDATA", "PATHEXT")


def test_posix_env_does_not_carry_windows_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    for name in _WINDOWS_VARS:
        monkeypatch.setenv(name, "x")
    assert_that(launch_env(None)).does_not_contain_key(*_WINDOWS_VARS)


def test_a_real_interpreter_starts_with_the_launch_env() -> None:
    code = "import socket, ssl, asyncio, tempfile, pathlib; pathlib.Path.home(); tempfile.gettempdir(); print('ok')"
    result = subprocess.run(
        [sys.executable, "-c", code], env=launch_env(None), capture_output=True, text=True, timeout=60, check=False
    )
    assert_that(result.stdout.strip()).described_as(result.stderr).is_equal_to("ok")


@pytest.mark.parametrize("sdk_path", [None, Path("/sdk")])
def test_launch_env_points_at_bundled_fonts(sdk_path: Path | None) -> None:
    expected = Path(__file__).resolve().parents[3] / "src" / "decker" / "assets" / "fonts"
    env = launch_env(sdk_path)
    assert_that(env["DECKER_FONT_DIR"]).is_equal_to(str(expected))
    assert_that(Path(env["DECKER_FONT_DIR"]).is_absolute()).is_true()
    assert_that((Path(env["DECKER_FONT_DIR"]) / "DejaVuSans.ttf").is_file()).is_true()
