import json
from typing import Any

import pytest
from assertpy import assert_that

from decker.plugin.repository_index import (
    MAX_INDEX_BYTES,
    IndexVersion,
    RepositoryIndexError,
    latest_compatible,
    parse_index,
)

INDEX_URL = "https://valsr.github.io/decker-plugins/index.json"
SHA = "a" * 64


def _version(**over: Any) -> dict[str, Any]:
    base = {
        "version": "1.0.0",
        "decker": ">=0.2",
        "url": "https://example.com/p.deckerplugin",
        "size": 10,
        "sha256": SHA,
        "released": "2026-11-16T19:00:00Z",
        "contributes": {"actions": ["Counter"], "icon_packs": ["Counter icons"]},
    }
    base.update(over)
    return base


def _plugin(plugin_id: str = "com.decker.counter", versions: list[Any] | None = None, **over: Any) -> dict[str, Any]:
    base = {
        "id": plugin_id,
        "name": "Counter",
        "author": "Decker",
        "description": "Counts key presses.",
        "category": "Utilities",
        "homepage": "https://github.com/valsr/decker-plugins",
        "icon": f"icons/{plugin_id}.png",
        "versions": [_version()] if versions is None else versions,
    }
    base.update(over)
    return base


def _index(plugins: list[Any], **over: Any) -> bytes:
    doc = {"format": 1, "name": "Decker Plugins", "generated": "2026-11-16T19:00:00Z", "plugins": plugins}
    doc.update(over)
    return json.dumps(doc).encode()


def test_parses_spec_example() -> None:
    index = parse_index(_index([_plugin()]), INDEX_URL)
    assert_that(index.name).is_equal_to("Decker Plugins")
    assert_that(index.plugins).is_length(1)
    plugin = index.plugins[0]
    assert_that(plugin.icon_url).is_equal_to("https://valsr.github.io/decker-plugins/icons/com.decker.counter.png")
    assert_that(plugin.versions).is_length(1)
    assert_that(plugin.versions[0].contributes["actions"]).is_equal_to(["Counter"])
    assert_that(index.warnings).is_empty()


def test_rejects_unknown_format() -> None:
    with pytest.raises(RepositoryIndexError):
        parse_index(_index([], format=2), INDEX_URL)


def test_rejects_oversize() -> None:
    with pytest.raises(RepositoryIndexError):
        parse_index(b" " * (MAX_INDEX_BYTES + 1), INDEX_URL)


@pytest.mark.parametrize("data", [b"[]", b"not json", b'{"format": 1}'])
def test_rejects_non_object_json(data: bytes) -> None:
    with pytest.raises(RepositoryIndexError):
        parse_index(data, INDEX_URL)


def test_skips_bad_entries_with_warning() -> None:
    plugins = [
        _plugin("../x"),
        _plugin("com.a.one", [_version(version="1.0")]),
        _plugin("com.a.two", [_version(sha256="zz")]),
        _plugin("com.a.three", [_version(size=-1)]),
        _plugin("com.a.four", [_version(url="ftp://example.com/p")]),
        _plugin("com.a.five", [_version(url="file:///etc/passwd")]),
        _plugin("com.a.good"),
    ]
    index = parse_index(_index(plugins), INDEX_URL)
    assert_that([p.id for p in index.plugins]).is_equal_to(["com.a.good"])
    assert_that(index.warnings).is_length(6)


def test_file_urls_allowed_for_file_index() -> None:
    index = parse_index(_index([_plugin(versions=[_version(url="p/a.deckerplugin")])]), "file:///tmp/r/index.json")
    assert_that(index.plugins[0].versions[0].url).is_equal_to("file:///tmp/r/p/a.deckerplugin")


def test_duplicate_ids_keep_first() -> None:
    index = parse_index(_index([_plugin(name="First"), _plugin(name="Second")]), INDEX_URL)
    assert_that(index.plugins).is_length(1)
    assert_that(index.plugins[0].name).is_equal_to("First")
    assert_that(index.warnings).is_length(1)


def test_versions_sorted_newest_first() -> None:
    versions = [_version(version=v) for v in ("1.2.0", "1.10.0", "1.9.0")]
    index = parse_index(_index([_plugin(versions=versions)]), INDEX_URL)
    assert_that([v.version for v in index.plugins[0].versions]).is_equal_to(["1.10.0", "1.9.0", "1.2.0"])


def test_plugin_without_valid_versions_skipped() -> None:
    index = parse_index(_index([_plugin(versions=[]), _plugin("com.a.b", [_version(size=0)])]), INDEX_URL)
    assert_that(index.plugins).is_empty()
    assert_that(index.warnings).is_not_empty()


def test_strings_are_stripped_and_capped() -> None:
    index = parse_index(_index([_plugin(name="  " + "n" * 200, description="d" * 3000)]), INDEX_URL)
    assert_that(index.plugins[0].name).is_length(100)
    assert_that(index.plugins[0].description).is_length(2000)


def test_malformed_urls_skip_the_entry_not_the_index() -> None:
    doc = {
        "format": 1,
        "plugins": [
            {
                "id": "com.t.bad",
                "versions": [{"version": "1.0.0", "url": "http://[bad/x", "size": 1, "sha256": "0" * 64}],
            },
            {
                "id": "com.t.ok",
                "icon": "http://[bad",
                "versions": [{"version": "1.0.0", "url": "ok.zip", "size": 1, "sha256": "0" * 64}],
            },
        ],
    }
    index = parse_index(json.dumps(doc).encode(), "http://example.com/index.json")
    assert_that([p.id for p in index.plugins]).is_equal_to(["com.t.ok"])
    assert_that(index.plugins[0].icon_url).is_none()


def test_latest_compatible_skips_incompatible_versions() -> None:
    def version(number: str, decker: str | None) -> IndexVersion:
        return IndexVersion(number, decker, "http://x/y", 1, "0" * 64, "", {})

    versions = (version("3.0.0", ">=99"), version("2.0.0", None), version("1.0.0", None))
    best = latest_compatible(versions)
    assert best is not None
    assert_that(best.version).is_equal_to("2.0.0")
    assert_that(latest_compatible((version("3.0.0", ">=99"),))).is_none()


def test_index_version_without_platform_fields_supports_all() -> None:
    index = parse_index(_index([_plugin()]), INDEX_URL)
    assert_that(index.plugins[0].versions[0].platforms).is_none()


def test_index_version_parses_platform_fields() -> None:
    entry = _version(platforms=["linux"], linux={"desktops": ["kde"], "sessions": ["wayland"]})
    index = parse_index(_index([_plugin(versions=[entry])]), INDEX_URL)
    spec = index.plugins[0].versions[0].platforms
    assert spec is not None
    assert_that(spec.os).is_equal_to(("linux",))
    assert_that(spec.desktops).is_equal_to(("kde",))
    assert_that(spec.sessions).is_equal_to(("wayland",))


def test_invalid_platform_data_skips_only_that_version() -> None:
    bad = _version(version="2.0.0", platforms="linux")
    index = parse_index(_index([_plugin(versions=[_version(), bad])]), INDEX_URL)
    assert_that(index.plugins).is_length(1)
    assert_that([v.version for v in index.plugins[0].versions]).is_equal_to(["1.0.0"])
    assert_that(index.warnings).is_length(1)
    assert_that(index.warnings[0]).contains("com.decker.counter").contains("invalid platform data")


def test_index_icon_may_be_a_builtin_ref() -> None:
    index = parse_index(_index([_plugin(icon="fas:toolbox")]), INDEX_URL)
    plugin = index.plugins[0]
    assert_that(plugin.icon_ref).is_equal_to("fas:toolbox")
    assert_that(plugin.icon_url).is_none()
    assert_that(index.warnings).is_empty()


def test_index_png_icon_still_gives_url() -> None:
    plugin = parse_index(_index([_plugin()]), INDEX_URL).plugins[0]
    assert_that(plugin.icon_url).is_not_none()
    assert_that(plugin.icon_ref).is_none()
