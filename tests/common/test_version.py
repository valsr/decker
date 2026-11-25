import pytest
from assertpy import assert_that

from decker.common import version


def test_none_specifier_is_compatible():
    assert_that(version.is_compatible(None, "0.1.0")).is_true()
    assert_that(version.is_compatible("", "0.1.0")).is_true()


def test_prerelease_matches():
    assert_that(version.is_compatible(">=0.2", "0.3.0.dev1")).is_true()


def test_excluded():
    assert_that(version.is_compatible(">=1.0", "0.2.0")).is_false()


def test_invalid_specifier_is_incompatible():
    assert_that(version.is_compatible("not a spec", "0.2.0")).is_false()


def test_default_uses_decker_version(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(version, "decker_version", lambda: "0.5.0")
    assert_that(version.is_compatible(">=0.5")).is_true()
    assert_that(version.is_compatible(">=0.6")).is_false()


def test_invalid_version_is_incompatible():
    assert_that(version.is_compatible(">=1.0", "garbage")).is_false()


def test_decker_version_falls_back_to_zero(monkeypatch: pytest.MonkeyPatch):
    def _missing(_name: str) -> str:
        raise version.metadata.PackageNotFoundError

    monkeypatch.setattr(version.metadata, "version", _missing)
    assert_that(version.decker_version()).is_equal_to("0")


def test_package_declares_0_8_1():
    import tomllib
    from pathlib import Path

    data = tomllib.loads((Path(__file__).parents[2] / "pyproject.toml").read_text(encoding="utf-8"))
    assert_that(data["tool"]["poetry"]["version"]).is_equal_to("0.8.1")


def test_gates_across_the_0_3_bump():
    assert_that(version.is_compatible(">=0.2", "0.3.0")).is_true()
    assert_that(version.is_compatible(">=0.3", "0.2.0")).is_false()
    assert_that(version.is_compatible(">=0.3", "0.3.0")).is_true()
    assert_that(version.is_compatible(">=0.4", "0.3.0")).is_false()
    assert_that(version.is_compatible(">=0.4", "0.4.0")).is_true()
