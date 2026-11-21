from typing import Any

import pytest
from assertpy import assert_that

from decker.common.options import clean_options
from decker_plugin_sdk import _options

_CASES: dict[str, list[Any]] = {
    "valid": [{"label": "A", "value": "a"}, {"label": "B", "value": "b"}],
    "empty": [],
    "missing_keys": [{"label": "A"}, {"value": "b"}, {}, {"label": "C", "value": "c"}],
    "non_strings": [{"label": 1, "value": "a"}, {"label": "B", "value": 2}, {"label": None, "value": None}],
    "long_text": [{"label": "L" * 300, "value": "v" * 300}],
    "duplicates": [{"label": "A", "value": "x"}, {"label": "B", "value": "x"}, {"label": "C", "value": "y"}],
    "duplicates_after_cut": [{"label": "A", "value": "v" * 256 + "1"}, {"label": "B", "value": "v" * 256 + "2"}],
    "exactly_256": [{"label": "l" * 256, "value": "v" * 256}],
    "exactly_257": [{"label": "l" * 257, "value": "v" * 257}],
    "over_cap": [{"label": f"l{i}", "value": f"v{i}"} for i in range(1000)],
    "not_dicts": [None, 3, "s", ["a", "b"], {"label": "ok", "value": "ok"}],
    "empty_strings": [{"label": "", "value": ""}, {"label": "", "value": ""}],
}


@pytest.mark.parametrize("name", list(_CASES))
def test_option_cleaning_matches_the_server(name: str) -> None:
    raw = _CASES[name]
    assert_that(_options.clean(raw)).is_equal_to(clean_options(raw))


def test_limits_match_the_server() -> None:
    from decker.common import options as server  # pylint: disable=import-outside-toplevel

    assert_that(_options.MAX_OPTIONS).is_equal_to(server.MAX_OPTIONS)
    assert_that(_options.MAX_TEXT).is_equal_to(server.MAX_TEXT)
