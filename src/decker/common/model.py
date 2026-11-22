"""Common model classes."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal


@dataclass
class KeyInfo:
    """Layout and pixel dimensions for a single button key."""

    row: int
    col: int
    pixel_width: int
    pixel_height: int
    type: str  # "key", "dial", "touchscreen"


@dataclass
class DeviceHardwareInfo:
    """Hardware info for a Stream Deck device."""

    id: str | None
    type: str
    vendor_id: int
    product_id: int
    num_keys: int
    keys: list[KeyInfo] = field(default_factory=list)
    serial: str | None = None
    path: str = ""
    firmware: str | None = None


class KeyPressState(StrEnum):
    """Physical and gesture state of a key."""

    UP = "up"
    DOWN = "down"
    HELD = "held"
    DOUBLE_PRESSED = "double_pressed"
    TRIPLE_PRESSED = "triple_pressed"


@dataclass
class KeyState:
    """Full runtime state for a single key on a Stream Deck device."""

    key_index: int
    row: int
    col: int
    state: KeyPressState
    image_b64: str | None = None  # base64 PNG of the last image sent to this key
    label: str = ""
    plugin_uuid: str | None = None
    card_uuid: str | None = None
    context_id: str | None = None
    settings: dict[str, Any] = field(default_factory=dict)
    appearance: dict[str, Any] = field(default_factory=dict)
    appearance_defaults: dict[str, Any] = field(default_factory=dict)
    bindings: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PageInfo:
    """One page of key assignments on a device."""

    page_id: str
    name: str


@dataclass(frozen=True)
class ProfileInfo:
    """One profile of a device: a named set of pages. ``icon`` is an icon ref; empty means the Decker icon."""

    profile_id: int
    name: str
    icon: str = ""
    icon_color: str = ""  # fill colour of an SVG icon; empty means the default


@dataclass(frozen=True)
class DeviceProfiles:
    """A device's profiles in dropdown order and the one currently active."""

    device_id: str
    profiles: list[ProfileInfo]
    active_profile_id: int


@dataclass(frozen=True)
class DevicePages:
    """A device's pages in display order, its default page, and the page currently on the hardware."""

    device_id: str
    pages: list[PageInfo]
    default_page_id: str
    active_page_id: str
    profile_id: int = 0


@dataclass(frozen=True)
class WindowInfo:
    """The focused window's attributes; any string may be empty when a backend cannot supply it."""

    window_class: str = ""
    window_type: str = ""
    title: str = ""
    executable: str = ""
    desktop_file: str = ""
    pid: int = 0


MATCH_FIELDS: tuple[str, ...] = ("window_class", "window_type", "title", "executable", "desktop_file")
MATCH_OPS: tuple[str, ...] = ("equals", "contains", "not_contains", "regex")


@dataclass(frozen=True)
class MatchCondition:
    """One field/operator/value test against a window."""

    field: str
    op: str
    value: str


@dataclass(frozen=True)
class AppRule:
    """Activates a profile (and optionally a page) when all conditions match the focused window."""

    rule_id: str
    name: str
    enabled: bool
    conditions: list[MatchCondition]
    profile_id: int
    page_id: str = ""


@dataclass(frozen=True)
class DefaultRule:
    """Target used when no app rule matches; it has no conditions."""

    enabled: bool = False
    profile_id: int = 0
    page_id: str = ""


@dataclass(frozen=True)
class DeviceRules:
    """A device's ordered app rules plus its default rule."""

    device_id: str
    rules: list[AppRule]
    default_rule: DefaultRule


@dataclass(frozen=True)
class RuleTarget:
    """The profile and page a matched rule selects (empty page_id means the profile's default page)."""

    profile_id: int
    page_id: str


@dataclass(frozen=True)
class ScreensaverSettings:
    """A device's brightness and screensaver configuration."""

    brightness: int = 100
    timeout_minutes: int = 0
    saver_brightness: int = 10
    content: Literal["none", "image", "gallery"] = "none"
    image_path: str = ""
    folder_path: str = ""
    scaling: Literal["center", "stretch"] = "center"
    bezel: bool = False
    key_mm: float = 16.0
    gap_mm: float = 4.0
    gallery_mode: Literal["activation", "interval"] = "activation"
    gallery_minutes: int = 5
