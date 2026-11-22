import copy
import json
from typing import Any

import pytest
from assertpy import assert_that

from decker.common.platforms import PlatformSpec
from decker.plugin.manifest import (
    ManifestValidationError,
    PluginManifest,
    declared_options_sources,
)
from decker.plugin.settings_schema import SettingField

_COUNTER: dict[str, Any] = {
    "format": 3,
    "id": "com.decker.counter",
    "name": "Counter",
    "version": "1.0.0",
    "author": "Decker",
    "description": "Counts key presses.",
    "homepage": "https://github.com/valsr/Decker",
    "icon": "icons/plugin",
    "category": "Utilities",
    "decker": ">=0.7",
    "runtime": {"type": "python", "entry": "plugin.py", "python": ">=3.12", "requirements": []},
    "permissions": [],
    "global_settings": [],
    "contributes": {
        "icon_packs": [{"id": "com.decker.counter.icons", "name": "Counter icons", "path": "iconpack"}],
        "cards": [
            {
                "id": "com.decker.counter.counter",
                "name": "Counter",
                "tooltip": "Press to count, long press to reset",
                "icon": "icons/counter",
                "states": [{"image": "icons/counter"}],
                "settings": [
                    {"section": "Counting"},
                    {"key": "step", "type": "int", "label": "Step", "default": 1, "min": 1, "max": 9999},
                    {
                        "key": "double_step",
                        "type": "int",
                        "label": "Double press step",
                        "optional": True,
                        "placeholder": "None (presses register immediately)",
                    },
                    {
                        "key": "max",
                        "type": "int",
                        "label": "Max count",
                        "default": 0,
                        "min": 0,
                        "special_value": "∞ (no limit)",
                    },
                ],
            }
        ],
    },
}


def _counter(**overrides: Any) -> dict[str, Any]:
    data = copy.deepcopy(_COUNTER)
    data.update(overrides)
    return data


def _with_action(**action: Any) -> dict[str, Any]:
    data = _counter()
    data["contributes"]["cards"][0].update(action)
    return data


def test_counter_manifest_parses() -> None:
    manifest = PluginManifest.from_dict(_counter())
    assert_that(manifest.format).is_equal_to(3)
    assert_that(manifest.cards[0].id).is_equal_to("com.decker.counter.counter")
    assert_that(manifest.cards[0].bindings).contains_key("press", "double_press", "long_press")
    assert_that(manifest.cards[0].settings).is_length(4)
    assert_that(manifest.runtime).is_not_none()
    assert manifest.runtime is not None
    assert_that(manifest.runtime.type).is_equal_to("python")
    assert_that(manifest.runtime.entry).is_equal_to("plugin.py")
    assert_that(manifest.icon_packs[0].path).is_equal_to("iconpack")


def test_unknown_format_rejected() -> None:
    with pytest.raises(ManifestValidationError, match="format 4"):
        PluginManifest.from_dict(_counter(format=4))


@pytest.mark.parametrize("fmt", [1, 2])
def test_format_2_manifest_is_refused_naming_the_format(fmt: int) -> None:
    with pytest.raises(ManifestValidationError, match=f"format {fmt} is no longer supported.*format 3"):
        PluginManifest.from_dict(_counter(format=fmt))


def test_missing_format_rejected() -> None:
    data = _counter()
    del data["format"]
    with pytest.raises(ManifestValidationError, match="format"):
        PluginManifest.from_dict(data)


def test_unknown_top_level_keys_ignored() -> None:
    manifest = PluginManifest.from_dict(_counter(signature="x", future={"a": 1}))
    assert_that(manifest.id).is_equal_to("com.decker.counter")


@pytest.mark.parametrize("contributes", [{}, [], None])
def test_contributes_required_and_non_empty(contributes: Any) -> None:
    with pytest.raises(ManifestValidationError, match="contributes"):
        PluginManifest.from_dict(_counter(contributes=contributes))


def test_unknown_contribution_kind_rejected() -> None:
    with pytest.raises(ManifestValidationError, match="requires a newer version of Decker"):
        PluginManifest.from_dict(_counter(contributes={"screensavers": []}))


def test_actions_require_runtime() -> None:
    data = _counter()
    del data["runtime"]
    with pytest.raises(ManifestValidationError, match="runtime"):
        PluginManifest.from_dict(data)


def test_icon_pack_only_needs_no_runtime() -> None:
    data = _counter()
    del data["runtime"]
    del data["contributes"]["cards"]
    manifest = PluginManifest.from_dict(data)
    assert_that(manifest.runtime).is_none()
    assert_that(manifest.cards).is_empty()


def test_contribution_id_must_be_prefixed_by_plugin_id() -> None:
    with pytest.raises(ManifestValidationError, match="com.decker.counter."):
        PluginManifest.from_dict(_with_action(id="com.other.x"))
    data = _counter()
    data["contributes"]["icon_packs"][0]["id"] = "com.other.icons"
    with pytest.raises(ManifestValidationError, match="icon pack id"):
        PluginManifest.from_dict(data)


@pytest.mark.parametrize("req", ["foo @ https://x/y.whl", "git+https://x"])
def test_url_requirement_rejected(req: str) -> None:
    data = _counter()
    data["runtime"]["requirements"] = [req]
    with pytest.raises(ManifestValidationError):
        PluginManifest.from_dict(data)


def test_index_requirement_accepted() -> None:
    data = _counter()
    data["runtime"]["requirements"] = ["requests>=2"]
    manifest = PluginManifest.from_dict(data)
    assert manifest.runtime is not None
    assert_that(manifest.runtime.requirements).is_equal_to(("requests>=2",))


def test_runtime_type_must_be_python_or_executable() -> None:
    data = _counter()
    data["runtime"]["type"] = "java"
    with pytest.raises(ManifestValidationError, match="runtime type"):
        PluginManifest.from_dict(data)


def test_invalid_settings_schema_becomes_manifest_error() -> None:
    with pytest.raises(ManifestValidationError, match="settings"):
        PluginManifest.from_dict(_with_action(settings="nope"))
    with pytest.raises(ManifestValidationError, match="global_settings"):
        PluginManifest.from_dict(_counter(global_settings="nope"))


def test_load_valid_file(tmp_path: Any) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(_counter()), encoding="utf-8")
    assert_that(PluginManifest.load(path).name).is_equal_to("Counter")


def test_malformed_json_and_non_object_rejected(tmp_path: Any) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ManifestValidationError, match="malformed"):
        PluginManifest.load(bad)
    arr = tmp_path / "arr.json"
    arr.write_text("[]", encoding="utf-8")
    with pytest.raises(ManifestValidationError, match="JSON object"):
        PluginManifest.load(arr)
    with pytest.raises(ManifestValidationError, match="could not read"):
        PluginManifest.load(tmp_path / "missing.json")


def test_trailing_newline_ids_rejected() -> None:
    data = _counter()
    data["id"] = "com.x.y\n"
    with pytest.raises(ManifestValidationError):
        PluginManifest.from_dict(data)
    with pytest.raises(ManifestValidationError):
        PluginManifest.from_dict(_with_action(id="com.decker.counter.x\n"))


def test_float_format_rejected() -> None:
    with pytest.raises(ManifestValidationError, match="format"):
        PluginManifest.from_dict(_counter(format=1.0))


@pytest.mark.parametrize("version", ["1.0", "latest", "01.2.3", "1.2.3.4"])
def test_non_semver_version_rejected(version: str) -> None:
    with pytest.raises(ManifestValidationError, match="semantic version"):
        PluginManifest.from_dict(_counter(version=version))


@pytest.mark.parametrize("version", ["0.1.0", "1.2.3-beta.1", "1.0.0+build.5"])
def test_semver_versions_accepted(version: str) -> None:
    assert_that(PluginManifest.from_dict(_counter(version=version)).version).is_equal_to(version)


def test_page_in_global_settings_is_rejected() -> None:
    data = copy.deepcopy(_COUNTER)
    data["global_settings"] = [{"key": "where", "type": "page", "label": "Where"}]
    with pytest.raises(ManifestValidationError, match="where.*page"):
        PluginManifest.from_dict(data)


def test_page_in_action_settings_is_accepted() -> None:
    data = copy.deepcopy(_COUNTER)
    data["contributes"]["cards"][0]["settings"] = [{"key": "where", "type": "page", "label": "Where"}]
    manifest = PluginManifest.from_dict(data)
    field = manifest.cards[0].settings[0]
    assert isinstance(field, SettingField)
    assert_that(field.type).is_equal_to("page")


def _static_action(**extra: Any) -> dict[str, Any]:
    return {"id": "com.decker.counter.preset", "name": "Preset", "static": True, **extra}


def _without_runtime(actions: list[dict[str, Any]]) -> dict[str, Any]:
    data = _counter()
    del data["runtime"]
    data["contributes"] = {"cards": actions}
    return data


def test_static_action_without_runtime_is_valid() -> None:
    manifest = PluginManifest.from_dict(_without_runtime([_static_action()]))
    assert_that(manifest.runtime).is_none()
    assert_that(manifest.cards[0].static).is_true()


def test_non_static_action_without_runtime_is_still_rejected() -> None:
    plain = {"id": "com.decker.counter.plain", "name": "Plain"}
    with pytest.raises(ManifestValidationError, match="no runtime.*com.decker.counter.plain"):
        PluginManifest.from_dict(_without_runtime([_static_action(), plain]))


def test_mixed_actions_with_runtime() -> None:
    data = _counter()
    data["contributes"]["cards"].append(_static_action())
    manifest = PluginManifest.from_dict(data)
    assert_that([a.static for a in manifest.cards]).is_equal_to([False, True])


def test_static_must_be_boolean() -> None:
    with pytest.raises(ManifestValidationError, match="static"):
        PluginManifest.from_dict(_without_runtime([_static_action(static="yes")]))


def test_static_action_with_settings_rejected() -> None:
    action = _static_action(settings=[{"key": "a", "type": "bool"}])
    with pytest.raises(ManifestValidationError, match="static.*settings"):
        PluginManifest.from_dict(_without_runtime([action]))


def test_static_action_keeps_appearance_defaults_and_states() -> None:
    action = _static_action(appearance={"title": {"text": "{page_index}"}}, states=[{"title": "x"}])
    spec = PluginManifest.from_dict(_without_runtime([action])).cards[0]
    assert_that(spec.appearance).is_equal_to({"title": {"text": "{page_index}"}})
    assert_that(spec.states[0].title).is_equal_to("x")


def test_action_appearance_may_declare_an_icon_default() -> None:
    icon = {"ref": "pack:com.decker.counter.icons/x.svg", "color": "#ff0000"}
    manifest = PluginManifest.from_dict(_with_action(appearance={"icon": icon}))
    assert_that(manifest.cards[0].appearance).is_equal_to({"icon": icon})


def test_profile_in_global_settings_is_rejected() -> None:
    data = copy.deepcopy(_COUNTER)
    data["global_settings"] = [{"key": "who", "type": "profile", "label": "Who"}]
    with pytest.raises(ManifestValidationError, match="who.*profile"):
        PluginManifest.from_dict(data)


def test_manifest_without_platform_fields_has_no_spec() -> None:
    assert_that(PluginManifest.from_dict(_counter()).platforms).is_none()


def test_manifest_parses_platforms_and_linux_block() -> None:
    manifest = PluginManifest.from_dict(
        _counter(platforms=["linux", "windows"], linux={"desktops": ["kde"], "sessions": ["wayland"]})
    )
    assert_that(manifest.platforms).is_equal_to(PlatformSpec(("linux", "windows"), ("kde",), ("wayland",)))


@pytest.mark.parametrize(
    "overrides",
    [
        {"platforms": []},
        {"platforms": "linux"},
        {"platforms": ["linux", 3]},
        {"platforms": ["windows"], "linux": {"desktops": ["kde"]}},
        {"linux": {"desktops": []}},
        {"linux": "kde"},
    ],
)
def test_manifest_rejects_malformed_platform_values(overrides: dict[str, Any]) -> None:
    with pytest.raises(ManifestValidationError, match="^manifest "):
        PluginManifest.from_dict(_counter(**overrides))


def test_manifest_keeps_unknown_os_names() -> None:
    manifest = PluginManifest.from_dict(_counter(platforms=["Linux", "FreeBSD"]))
    assert_that(manifest.platforms).is_equal_to(PlatformSpec(("linux", "freebsd")))


def _minimal(fmt: int, contributes: dict) -> dict:
    return {
        "format": fmt,
        "id": "com.acme.demo",
        "name": "Demo",
        "version": "1.0.0",
        "author": "Acme",
        "contributes": contributes,
    }


def test_format_3_actions_are_not_cards() -> None:
    manifest = PluginManifest.from_dict(
        _minimal(3, {"actions": [{"id": "com.acme.demo.one", "name": "One"}], "icon_packs": []})
    )

    assert_that(manifest.cards).is_empty()
    assert_that([a.id for a in manifest.actions]).is_equal_to(["com.acme.demo.one"])


def _variables(entries: list[Any], **extra: Any) -> dict[str, Any]:
    data = _minimal(3, {"variables": entries})
    data.update({"runtime": {"type": "python", "entry": "plugin.py"}, "decker": ">=0.6", **extra})
    return data


def test_variables_parse() -> None:
    data = _variables([{"name": "media_volume", "description": "x"}, {"pattern": "media_{device}_volume"}])

    manifest = PluginManifest.from_dict(data)

    assert_that([(v.name, v.pattern, v.description) for v in manifest.variables]).is_equal_to(
        [("media_volume", None, "x"), (None, "media_{device}_volume", "")]
    )


def test_variables_need_runtime() -> None:
    data = _variables([{"name": "media_volume"}])
    del data["runtime"]

    with pytest.raises(ManifestValidationError, match="runtime"):
        PluginManifest.from_dict(data)


def test_variables_need_decker_constraint() -> None:
    data = _variables([{"name": "media_volume"}])
    del data["decker"]

    with pytest.raises(ManifestValidationError, match="decker"):
        PluginManifest.from_dict(data)


def test_variable_core_name_rejected() -> None:
    with pytest.raises(ManifestValidationError, match="page_name"):
        PluginManifest.from_dict(_variables([{"name": "page_name"}]))


def test_variable_pattern_matching_core_name_rejected() -> None:
    with pytest.raises(ManifestValidationError, match="page_"):
        PluginManifest.from_dict(_variables([{"pattern": "page_{x}"}]))


def test_duplicate_variable_in_plugin_rejected() -> None:
    with pytest.raises(ManifestValidationError, match="duplicate"):
        PluginManifest.from_dict(_variables([{"name": "a_b"}, {"name": "a_b"}]))


@pytest.mark.parametrize(
    "entry", [{"name": "A!"}, {"pattern": "no_placeholder"}, {"name": "a", "pattern": "a_{b}"}, {}, "text"]
)
def test_invalid_variable_entry_rejected(entry: Any) -> None:
    with pytest.raises(ManifestValidationError):
        PluginManifest.from_dict(_variables([entry]))


def test_too_many_variables_rejected() -> None:
    with pytest.raises(ManifestValidationError, match="at most 256"):
        PluginManifest.from_dict(_variables([{"name": f"v{i}"} for i in range(257)]))


def test_variables_only_plugin_is_valid() -> None:
    manifest = PluginManifest.from_dict(_variables([{"name": "media_volume"}]))

    assert_that(manifest.cards).is_empty()
    assert_that(manifest.actions).is_empty()
    assert_that(manifest.icon_packs).is_empty()
    assert_that(manifest.variables).is_length(1)


def test_plugin_contributing_nothing_is_rejected() -> None:
    with pytest.raises(ManifestValidationError, match="cards, actions, icon packs or variables"):
        PluginManifest.from_dict(_minimal(3, {"cards": []}))


@pytest.mark.parametrize(
    "entry",
    [
        {"name": None},
        {"pattern": None},
        {"name": None, "pattern": None},
        {"name": 5},
        {"name": "ok", "description": {}},
    ],
)
def test_null_or_mistyped_variable_values_rejected(entry: Any) -> None:
    with pytest.raises(ManifestValidationError):
        PluginManifest.from_dict(_variables([entry]))


_SOURCED_FIELD = {"key": "dev", "type": "choice", "label": "Device", "options_source": "devices"}


def _sourced(where: str, **extra: Any) -> dict[str, Any]:
    contributes: dict[str, Any] = {
        "cards": [{"id": "com.acme.demo.card", "name": "C", "settings": [_SOURCED_FIELD] if where == "card" else []}],
        "actions": [
            {"id": "com.acme.demo.act", "name": "A", "settings": [_SOURCED_FIELD] if where == "action" else []}
        ],
    }
    data = _minimal(3, contributes)
    data["runtime"] = {"type": "python", "entry": "plugin.py"}
    data["decker"] = ">=0.6"
    if where == "global":
        data["global_settings"] = [_SOURCED_FIELD]
    data.update(extra)
    return data


@pytest.mark.parametrize("where", ["card", "action", "global"])
def test_options_source_requires_a_decker_constraint(where: str) -> None:
    data = _sourced(where)
    del data["decker"]
    with pytest.raises(ManifestValidationError, match='options_source.*"decker"'):
        PluginManifest.from_dict(data)
    data["decker"] = "  "
    with pytest.raises(ManifestValidationError, match='options_source.*"decker"'):
        PluginManifest.from_dict(data)


def test_options_source_requires_a_runtime() -> None:
    data = _minimal(3, {"icon_packs": [{"id": "com.acme.demo.icons", "name": "I", "path": "icons"}]})
    data["global_settings"] = [_SOURCED_FIELD]
    data["decker"] = ">=0.6"
    with pytest.raises(ManifestValidationError, match="options_source.*no runtime"):
        PluginManifest.from_dict(data)


@pytest.mark.parametrize("where", ["card", "action", "global"])
def test_options_source_loads_with_decker_and_runtime(where: str) -> None:
    assert_that(PluginManifest.from_dict(_sourced(where)).id).is_equal_to("com.acme.demo")


def test_declared_options_sources_lists_every_scope() -> None:
    manifest = PluginManifest.from_dict(_sourced("global"))
    assert_that(declared_options_sources(manifest.global_settings, manifest.cards, manifest.actions)).is_equal_to(
        frozenset({"devices"})
    )


def test_manifest_without_options_source_needs_no_decker() -> None:
    data = _sourced("card")
    data["contributes"]["cards"][0]["settings"] = [{"key": "a", "type": "text", "label": "A"}]
    del data["decker"]
    assert_that(PluginManifest.from_dict(data).decker).is_none()


def _with_actions(*actions: dict[str, Any], **card: Any) -> dict[str, Any]:
    return {
        "format": 3,
        "id": "com.acme.demo",
        "name": "Demo",
        "version": "1.0.0",
        "author": "Acme",
        "runtime": {"type": "python", "entry": "plugin.py"},
        "contributes": {"actions": list(actions), "cards": [{"id": "com.acme.demo.card", "name": "C", **card}]},
    }


_ACT: dict[str, Any] = {"id": "com.acme.demo.act", "name": "Act"}


def test_action_scope_defaults_to_plugin() -> None:
    assert_that(PluginManifest.from_dict(_with_actions(_ACT)).actions[0].scope).is_equal_to("plugin")


def test_action_scope_global() -> None:
    manifest = PluginManifest.from_dict(_with_actions({**_ACT, "scope": "global"}))
    assert_that(manifest.actions[0].scope).is_equal_to("global")


def test_bad_scope_is_an_error() -> None:
    with pytest.raises(ManifestValidationError, match="scope"):
        PluginManifest.from_dict(_with_actions({**_ACT, "scope": "universe"}))


def test_returns_is_documentation_text() -> None:
    manifest = PluginManifest.from_dict(_with_actions({**_ACT, "returns": "the answer"}))
    assert_that(manifest.actions[0].returns).is_equal_to("the answer")
    assert_that(PluginManifest.from_dict(_with_actions(_ACT)).actions[0].returns).is_equal_to("")
    with pytest.raises(ManifestValidationError, match="returns"):
        PluginManifest.from_dict(_with_actions({**_ACT, "returns": 3}))


def test_card_bindings_parsed_with_defaults() -> None:
    manifest = PluginManifest.from_dict(
        _with_actions(_ACT, bindings={"press": {"action": "com.acme.demo.act", "args": {"a": 1}}})
    )
    bindings = manifest.cards[0].bindings
    assert_that(bindings["press"].action).is_equal_to("com.acme.demo.act")
    assert_that(dict(bindings["press"].args)).is_equal_to({"a": 1})
    assert_that(bindings["double_press"].is_nothing).is_true()
    assert_that(bindings["long_press"].is_nothing).is_true()


def test_card_gestures_key_is_rejected() -> None:
    with pytest.raises(ManifestValidationError, match="bindings instead of gestures"):
        PluginManifest.from_dict(_with_actions(_ACT, gestures=["press"]))


@pytest.mark.parametrize("key", ["card", "action", "action_settings", "behavior"])
def test_retired_card_keys_are_rejected(key: str) -> None:
    with pytest.raises(ManifestValidationError, match=key):
        PluginManifest.from_dict(_with_actions(_ACT, **{key: "x"}))


def test_binding_to_undeclared_own_action_is_an_error() -> None:
    with pytest.raises(ManifestValidationError, match="does not declare"):
        PluginManifest.from_dict(_with_actions(_ACT, bindings={"press": {"action": "com.acme.demo.missing"}}))


def test_binding_to_foreign_action_is_accepted() -> None:
    manifest = PluginManifest.from_dict(_with_actions(_ACT, bindings={"press": {"action": "com.other.act"}}))
    assert_that(manifest.cards[0].bindings["press"].action).is_equal_to("com.other.act")


def test_bad_binding_is_a_manifest_error() -> None:
    with pytest.raises(ManifestValidationError, match="bindings"):
        PluginManifest.from_dict(_with_actions(_ACT, bindings={"swipe": {"action": "com.other.act"}}))


def test_static_card_may_declare_bindings() -> None:
    data = _with_actions(_ACT, static=True, bindings={"press": {"action": "com.acme.demo.act"}})
    del data["runtime"]
    assert_that(PluginManifest.from_dict(data).cards[0].bindings["press"].action).is_equal_to("com.acme.demo.act")
