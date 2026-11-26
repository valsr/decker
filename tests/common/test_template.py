import time

import pytest
from assertpy import assert_that

from decker.common.template import TEMPLATE_VARIABLES, expand_template, template_names

VARS = {"page_name": "Home", "page_index": "2", "page_count": "5"}


def test_replaces_known_variables():
    assert_that(expand_template("{page_index} / {page_count} {page_name}", VARS)).is_equal_to("2 / 5 Home")


def test_unknown_names_are_left_as_typed():
    assert_that(expand_template("{nope} {page_name}", VARS)).is_equal_to("{nope} Home")


def test_double_braces_are_literal():
    assert_that(expand_template("{{page_name}}", VARS)).is_equal_to("{page_name}")
    assert_that(expand_template("}}", VARS)).is_equal_to("}")


def test_no_recursion():
    assert_that(expand_template("{page_name}", {"page_name": "{page_name}"})).is_equal_to("{page_name}")


def test_plain_text_is_unchanged():
    assert_that(expand_template("hello world", VARS)).is_equal_to("hello world")


@pytest.mark.parametrize(
    "text",
    ["{", "}", "{}", "{ page_name }", "{page_name", "page_name}", "{{{page_name}}}", "{page-name}", "{Page_Name}"],
)
def test_malformed_braces_never_raise(text: str):
    assert_that(expand_template(text, VARS)).is_instance_of(str)


def test_values_are_inserted_verbatim():
    value = "a{b}\\n\\1😀"
    assert_that(expand_template("[{page_name}]", {"page_name": value})).is_equal_to(f"[{value}]")


def test_empty_text_and_empty_variables():
    assert_that(expand_template("", VARS)).is_equal_to("")
    assert_that(expand_template("{page_name}", {})).is_equal_to("{page_name}")


def test_very_long_text_is_linear():
    start = time.monotonic()
    expand_template("{page_name} x " * 20000 + "{" * 1000, VARS)
    assert_that(time.monotonic() - start).is_less_than(2.0)


def test_variable_names_are_published():
    assert_that(TEMPLATE_VARIABLES).is_equal_to(
        ("page_name", "page_index", "page_count", "current_profile", "profile_id")
    )


def test_template_names() -> None:
    assert_that(template_names("a {x} {{y}} {z_1}")).is_equal_to(frozenset({"x", "z_1"}))


def test_dotted_references_expand() -> None:
    assert_that(expand_template("{press.pid}-{x}", {"press.pid": "42", "x": "a"})).is_equal_to("42-a")
    assert_that(expand_template("{{press.pid}}", {"press.pid": "42"})).is_equal_to("{press.pid}")
    assert_that(expand_template("{press.nope}", {})).is_equal_to("{press.nope}")


def test_template_names_include_dotted() -> None:
    assert_that(template_names("{press.step_1.pid} {a}")).is_equal_to({"press.step_1.pid", "a"})
