"""Bundled colour themes: one QSS template rendered with a per-theme colour palette."""

from dataclasses import asdict, dataclass
from pathlib import Path
from string import Template

from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication

_TEMPLATE_PATH: Path = Path(__file__).parent / "theme.qss"


@dataclass(frozen=True)
class ThemeColors:  # pylint: disable=too-many-instance-attributes
    """Every colour the stylesheet template uses, as #rrggbb strings."""

    window: str  # main window and dialog background
    surface: str  # panels: sidebar, tab pane, menus, tooltips
    base: str  # inputs and lists
    raised: str  # push buttons, inactive page tabs
    hover: str
    pressed: str
    border: str
    text: str
    text_muted: str
    text_disabled: str
    accent: str  # selection background
    accent_text: str  # text on accent
    accent_line: str  # focus rings, active tab marker
    error: str
    success: str
    key_bg: str
    key_border: str
    key_hover: str
    key_pressed: str
    key_text: str
    toast_bg: str
    toast_text: str
    icon: str = ""  # icon tint; empty means the text colour (not a stylesheet token)

    @property
    def icon_color(self) -> str:
        """The colour icons are tinted with: ``icon`` if set, else ``text``."""
        return self.icon or self.text


DARK = ThemeColors(
    window="#2b2b2e",
    surface="#252527",
    base="#1e1e20",
    raised="#3a3a3d",
    hover="#48484c",
    pressed="#2f2f32",
    border="#4a4a4f",
    text="#e8e8ea",
    text_muted="#a6a6ab",
    text_disabled="#6c6c71",
    accent="#0e639c",
    accent_text="#ffffff",
    accent_line="#3794ff",
    error="#ff7b6b",
    success="#5bd17a",
    key_bg="#2c2c2e",
    key_border="#1c1c1e",
    key_hover="#3a3a3c",
    key_pressed="#1c1c1e",
    key_text="#f5f5f7",
    toast_bg="#4a2421",
    toast_text="#ffd2cc",
)

LIGHT = ThemeColors(
    window="#f3f3f5",
    surface="#ebebee",
    base="#ffffff",
    raised="#fbfbfc",
    hover="#e0e0e4",
    pressed="#d2d2d7",
    border="#c4c4ca",
    text="#1c1c1e",
    text_muted="#5c5c63",
    text_disabled="#a3a3a8",
    accent="#0a64d6",
    accent_text="#ffffff",
    accent_line="#0a64d6",
    error="#b3261e",
    success="#1d7a3d",
    key_bg="#ffffff",
    key_border="#d1d1d6",
    key_hover="#e5e5ea",
    key_pressed="#d1d1d6",
    key_text="#1c1c1e",
    toast_bg="#fde7e5",
    toast_text="#8c1d16",
)

BUNDLED: dict[str, ThemeColors] = {"dark": DARK, "light": LIGHT}


def render_stylesheet(colors: ThemeColors) -> str:
    """Return the QSS template with every $token replaced by the palette's colour."""
    return Template(_TEMPLATE_PATH.read_text(encoding="utf-8")).substitute(asdict(colors))


def build_palette(colors: ThemeColors) -> QPalette:
    """Return a QPalette matching colors, for anything the stylesheet does not reach."""
    palette = QPalette()
    roles = {
        QPalette.ColorRole.Window: colors.window,
        QPalette.ColorRole.WindowText: colors.text,
        QPalette.ColorRole.Base: colors.base,
        QPalette.ColorRole.AlternateBase: colors.surface,
        QPalette.ColorRole.Text: colors.text,
        QPalette.ColorRole.Button: colors.raised,
        QPalette.ColorRole.ButtonText: colors.text,
        QPalette.ColorRole.BrightText: colors.error,
        QPalette.ColorRole.Highlight: colors.accent,
        QPalette.ColorRole.HighlightedText: colors.accent_text,
        QPalette.ColorRole.ToolTipBase: colors.surface,
        QPalette.ColorRole.ToolTipText: colors.text,
        QPalette.ColorRole.PlaceholderText: colors.text_muted,
        QPalette.ColorRole.Link: colors.accent_line,
        QPalette.ColorRole.Light: colors.hover,
        QPalette.ColorRole.Midlight: colors.raised,
        QPalette.ColorRole.Mid: colors.border,
        QPalette.ColorRole.Dark: colors.pressed,
        QPalette.ColorRole.Shadow: colors.key_border,
    }
    for role, color in roles.items():
        palette.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(colors.text_disabled))
    return palette


def apply_theme(app: QApplication, stylesheet: str, colors: ThemeColors | None) -> None:
    """Install stylesheet on app, with the matching palette (or the style's default for custom sheets)."""
    style = app.style()
    if colors is not None:
        app.setPalette(build_palette(colors))
    elif style is not None:
        app.setPalette(style.standardPalette())
    app.setProperty("iconColor", colors.icon_color if colors is not None else "")
    app.setStyleSheet(stylesheet)
