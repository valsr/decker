"""Scaffolding (`plugin new`) and packaging (`plugin pack`) of Decker plugins."""

import io
import os
import zipfile
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

from decker.cli.plugin import PluginCliError
from decker.plugin.manifest import ManifestValidationError, PluginManifest
from decker.plugin.package import PackageError, read_manifest

PACKAGE_SUFFIX = ".deckerplugin"
_TEMPLATE_PACKAGE = "decker.cli.templates"
_TEMPLATE_DIR = "plugin"
_EXCLUDED_DIRS = {".venv", "__pycache__"}


def _display_name(plugin_id: str) -> str:
    last = plugin_id.rsplit(".", maxsplit=1)[-1]
    return " ".join(word.capitalize() for word in last.replace("-", " ").replace("_", " ").split()) or last


def _render(text: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def scaffold(plugin_id: str, dest: Path) -> Path:
    """Create `<dest>/<plugin_id>/` from the plugin template. Refuses an existing folder."""
    try:
        PluginManifest.from_dict(
            {
                "format": 3,
                "id": plugin_id,
                "name": "x",
                "version": "0.1.0",
                "author": "x",
                "runtime": {"type": "python", "entry": "plugin.py"},
                "contributes": {"cards": [{"id": f"{plugin_id}.card", "name": "x"}]},
            }
        )
    except ManifestValidationError as exc:
        raise PluginCliError(f"invalid plugin id {plugin_id!r}: {exc}") from exc
    target = dest / plugin_id
    if target.exists():
        raise PluginCliError(f"{target} already exists")
    values = {"plugin_id": plugin_id, "card_id": f"{plugin_id}.card", "name": _display_name(plugin_id)}

    template_root = resources.files(_TEMPLATE_PACKAGE).joinpath(_TEMPLATE_DIR)
    files: dict[str, bytes] = {}
    stack: list[tuple[str, Traversable]] = [("", template_root)]
    while stack:
        prefix, node = stack.pop()
        for child in node.iterdir():
            rel = f"{prefix}{child.name}"
            if child.is_dir():
                if child.name != "__pycache__":
                    stack.append((f"{rel}/", child))
            elif child.name.endswith((".json", ".py")):
                files[rel] = _render(child.read_text(encoding="utf-8"), values).encode("utf-8")
            else:
                files[rel] = child.read_bytes()

    for rel, data in sorted(files.items()):
        path = target / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return target


def _included(folder: Path, path: Path) -> bool:
    parts = path.relative_to(folder).parts
    if any(part.startswith(".") or part in _EXCLUDED_DIRS for part in parts):
        return False
    return not (path.is_file() and path.suffix == PACKAGE_SUFFIX)


def _walk(folder: Path) -> list[Path]:
    """List every entry under folder without following symlinks; any symlink is an error."""
    found: list[Path] = []
    for root, dirs, files in os.walk(folder, followlinks=False):
        for name in sorted(dirs + files):
            path = Path(root) / name
            if path.is_symlink():
                raise PluginCliError(f"symlinks are not allowed in a plugin folder: {path}")
            found.append(path)
    return sorted(found)


def pack(folder: Path, out_dir: Path | None = None) -> Path:
    """Build `<id>-<version>.deckerplugin` from a plugin folder; an existing output file is overwritten."""
    if not folder.is_dir():
        raise PluginCliError(f"{folder} is not a folder")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in _walk(folder):
            if _included(folder, path) and path.is_file():
                zf.write(path, path.relative_to(folder).as_posix())
    data = buffer.getvalue()
    try:
        manifest = read_manifest(data)
    except PackageError as exc:
        raise PluginCliError(str(exc)) from exc

    out = (out_dir or Path.cwd()) / f"{manifest.id}-{manifest.version}{PACKAGE_SUFFIX}"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return out
