"""Coordinates page changes across the page store, key assignments, plugins and hardware."""

import json
import logging
import threading
from typing import TYPE_CHECKING

from decker.common.manager import Manager
from decker.common.model import DevicePages, DeviceProfiles, PageInfo, ProfileInfo

if TYPE_CHECKING:
    from decker.server.server_context import ServerContext

PAGES_CHANGED_EVENT = "pages_changed"
PROFILES_CHANGED_EVENT = "profiles_changed"
_APP_RULES_CHANGED_EVENT = "app_rules_changed"


class PageController(Manager):
    """Tracks which page and profile each device shows and performs page and profile lifecycle changes.

    Only the active page's cards are live: switching pages deactivates the old page's cards,
    redraws the hardware from the new page's cached images, then activates the new page's cards.
    Every change pushes a pages_changed event so clients refresh (profile changes also push profiles_changed). Active
    pages are held in memory and always belong to the device's active profile, which the page store persists; each
    device starts on its active profile's default page.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active: dict[str, str] = {}
        self._context: "ServerContext | None" = None

    def create(self, context: "ServerContext") -> None:
        """Bind the server context."""
        self._context = context

    def destroy(self) -> None:
        """Forget the active pages."""
        with self._lock:
            self._active.clear()

    @property
    def _ctx(self) -> "ServerContext":
        assert self._context is not None, "PageController.create() must be called before use"
        return self._context

    def active_page_id(self, device_id: str) -> str:
        """The page shown on the device; its default page until another is activated.

        Lock-free once a page is recorded: hardware/plugin paths call this while holding their own
        locks, so taking the controller lock here would invert the lock order against page switches.
        """
        recorded = self._active.get(device_id)
        if recorded is not None and self._ctx.page_store.get(device_id, recorded) is not None:
            return recorded
        with self._lock:
            page_id = self._active.get(device_id)
            if page_id is None or not self._in_active_profile(device_id, page_id):
                page_id = self._ctx.page_store.default_page_id(device_id)
                self._active[device_id] = page_id
            return page_id

    def active_profile_id(self, device_id: str) -> int:
        """The profile shown on the device."""
        return self._ctx.page_store.active_profile_id(device_id)

    def device_pages(self, device_id: str, profile_id: int | None = None) -> DevicePages | None:
        """Return a profile's pages, its default page and the active page (the active profile's when profile_id is None).

        A profile other than the active one has no active page (empty id); an unknown profile returns None.
        """
        with self._lock:
            store = self._ctx.page_store
            active_profile = store.active_profile_id(device_id)
            wanted = active_profile if profile_id is None else profile_id
            if store.get_profile(device_id, wanted) is None:
                return None
            return DevicePages(
                device_id=device_id,
                pages=store.list_pages(device_id, wanted),
                default_page_id=store.default_page_id(device_id, wanted),
                active_page_id=self.active_page_id(device_id) if wanted == active_profile else "",
                profile_id=wanted,
            )

    def device_profiles(self, device_id: str) -> DeviceProfiles:
        """Return the device's profiles in dropdown order and its active profile."""
        with self._lock:
            store = self._ctx.page_store
            return DeviceProfiles(
                device_id=device_id,
                profiles=store.list_profiles(device_id),
                active_profile_id=store.active_profile_id(device_id),
            )

    def restore(self, device_id: str) -> None:
        """Show the device's active page and activate its cards (used at server start)."""
        with self._lock:
            self._show(device_id, self.active_page_id(device_id))

    def activate(self, device_id: str, page_id: str) -> bool:
        """Switch the device to page_id, changing profile first if the page is in another one."""
        with self._lock:
            profile_id = self._ctx.page_store.profile_of_page(device_id, page_id)
            if profile_id is None:
                return False
            changed_profile = self._switch(device_id, page_id, profile_id)
        self._notify(device_id, profiles=changed_profile)
        return True

    def activate_profile(self, device_id: str, profile_id: int) -> bool:
        """Switch the device to a profile's default page. Returns False for an unknown profile."""
        with self._lock:
            store = self._ctx.page_store
            if store.get_profile(device_id, profile_id) is None:
                return False
            if store.active_profile_id(device_id) == profile_id:
                return True
            self._switch(device_id, store.default_page_id(device_id, profile_id), profile_id)
        self._notify(device_id, profiles=True)
        return True

    def add_profile(self, device_id: str, name: str | None = None) -> ProfileInfo:
        """Append a new profile with one page; it does not become active."""
        profile = self._ctx.page_store.add_profile(device_id, name)
        self._notify(device_id, profiles=True)
        return profile

    def rename_profile(self, device_id: str, profile_id: int, name: str) -> bool:
        """Rename a profile. Returns False for an unknown profile or a blank name."""
        renamed = self._ctx.page_store.rename_profile(device_id, profile_id, name)
        if renamed:
            self._notify(device_id, profiles=True)
        return renamed

    def set_profile_icon(self, device_id: str, profile_id: int, icon: str, color: str = "") -> bool:
        """Set a profile's icon ref ("" = the Decker icon) and fill colour. Returns False for an unknown profile."""
        updated = self._ctx.page_store.set_profile_icon(device_id, profile_id, icon, color)
        if updated:
            self._notify(device_id, profiles=True)
        return updated

    def delete_profile(self, device_id: str, profile_id: int) -> bool:
        """Delete a profile with its pages, keys and cached state. Refuses the last profile.

        Deleting the active profile first switches the device to the next profile (or the previous one if it
        was the last).
        """
        with self._lock:
            store = self._ctx.page_store
            ids = [profile.profile_id for profile in store.list_profiles(device_id)]
            if profile_id not in ids or len(ids) == 1:
                return False
            if store.active_profile_id(device_id) == profile_id:
                position = ids.index(profile_id)
                neighbour = ids[position + 1] if position + 1 < len(ids) else ids[position - 1]
                self._switch(device_id, store.default_page_id(device_id, neighbour), neighbour)
            for page_id in store.delete_profile(device_id, profile_id):
                self._ctx.key_assignment_store.delete_page(device_id, page_id)
                self._ctx.device_state_store.delete_page_state(device_id, page_id)
            rules = self._ctx.app_rule_store
            fallback = min(pid for pid in ids if pid != profile_id)
            rules_changed = rules is not None and rules.profile_deleted(device_id, profile_id, fallback)
        self._notify(device_id, profiles=True)
        if rules_changed:
            self._notify_rules(device_id)
        return True

    def clone_profile(self, device_id: str, profile_id: int) -> ProfileInfo | None:
        """Copy a profile with its pages and keys (new card instances); the copy does not become active."""
        with self._lock:
            cloned = self._ctx.page_store.clone_profile(device_id, profile_id)
            if cloned is None:
                return None
            clone, mapping = cloned
            for source, target in mapping.items():
                self._ctx.key_assignment_store.copy_page(device_id, source, target, mapping)
                self._ctx.device_state_store.copy_page_state(device_id, source, target)
        self._notify(device_id, profiles=True)
        return clone

    def add_page(self, device_id: str, name: str | None = None) -> PageInfo:
        """Append a new empty page; it does not become active."""
        page = self._ctx.page_store.add_page(device_id, name)
        self._notify(device_id)
        return page

    def rename_page(self, device_id: str, page_id: str, name: str) -> bool:
        """Rename a page. Returns False for an unknown page or a blank name."""
        renamed = self._ctx.page_store.rename_page(device_id, page_id, name)
        if renamed:
            self._notify(device_id)
        return renamed

    def move_page(self, device_id: str, page_id: str, new_index: int) -> bool:
        """Move a page within its profile. Returns False for an unknown page; a no-op move succeeds silently."""
        store = self._ctx.page_store
        profile_id = store.profile_of_page(device_id, page_id)
        if profile_id is None:
            return False
        before = store.list_pages(device_id, profile_id)
        moved = store.move_page(device_id, page_id, new_index)
        if moved and store.list_pages(device_id, profile_id) != before:
            self._notify(device_id)
        return moved

    def set_default_page(self, device_id: str, page_id: str) -> bool:
        """Make page_id the page shown when the server starts. Returns False for an unknown page."""
        updated = self._ctx.page_store.set_default_page(device_id, page_id)
        if updated:
            self._notify(device_id)
        return updated

    def delete_page(self, device_id: str, page_id: str) -> bool:
        """Delete a page with its assignments and cached key state. Refuses the last page.

        Deleting the active page first switches the device to the next page (or the previous one
        if it was the last).
        """
        with self._lock:
            profile_id = self._ctx.page_store.profile_of_page(device_id, page_id)
            if profile_id is None:
                return False
            ids = [page.page_id for page in self._ctx.page_store.list_pages(device_id, profile_id)]
            if len(ids) == 1:
                return False
            if self.active_page_id(device_id) == page_id:
                position = ids.index(page_id)
                neighbour = ids[position + 1] if position + 1 < len(ids) else ids[position - 1]
                self._hide(device_id, page_id)
                self._active[device_id] = neighbour
                self._show(device_id, neighbour)
            self._ctx.key_assignment_store.delete_page(device_id, page_id)
            self._ctx.device_state_store.delete_page_state(device_id, page_id)
            self._ctx.page_store.delete_page(device_id, page_id)
            rules = self._ctx.app_rule_store
            rules_changed = rules is not None and rules.page_deleted(device_id, page_id)
        self._notify(device_id)
        if rules_changed:
            self._notify_rules(device_id)
        return True

    def _in_active_profile(self, device_id: str, page_id: str) -> bool:
        store = self._ctx.page_store
        return store.profile_of_page(device_id, page_id) == store.active_profile_id(device_id)

    def _switch(self, device_id: str, page_id: str, profile_id: int) -> bool:
        """Show page_id (of profile_id) on the device; returns whether the profile changed. Caller holds the lock."""
        store = self._ctx.page_store
        current = self.active_page_id(device_id)
        changed_profile = store.active_profile_id(device_id) != profile_id
        if current != page_id:
            self._hide(device_id, current)
            if changed_profile:
                store.set_active_profile(device_id, profile_id)
            self._active[device_id] = page_id
            self._show(device_id, page_id)
        return changed_profile

    def _hide(self, device_id: str, page_id: str) -> None:
        plugins = self._ctx.plugin_manager
        for _key_index, assignment in self._ctx.key_assignment_store.all_for_page(device_id, page_id):
            plugins.deactivate_card(assignment.target)

    def _show(self, device_id: str, page_id: str) -> None:
        drawn = self._ctx.device_manager.show_page(device_id, page_id)
        if not drawn:
            logging.warning("PageController: device %s is not available; page %s not drawn", device_id, page_id)
        state = self._ctx.device_manager.get_device_state(device_id) if drawn else None
        plugins = self._ctx.plugin_manager
        for key_index, assignment in self._ctx.key_assignment_store.all_for_page(device_id, page_id):
            target = assignment.target
            plugins.store_settings(target, assignment.settings)
            plugins.set_bindings(target, assignment.bindings)
            # a key whose last image is already on the hardware is not redrawn bare before its plugin answers
            cached = state is not None and state.has_image(key_index)
            plugins.activate_card(target, assignment.appearance, draw=not cached)

    def _notify(self, device_id: str, profiles: bool = False) -> None:
        payload = json.dumps({"device_id": device_id})
        self._ctx.event_bus.push(PAGES_CHANGED_EVENT, payload)
        if profiles:
            self._ctx.event_bus.push(PROFILES_CHANGED_EVENT, payload)
        self._ctx.plugin_manager.refresh_page_dependent(device_id)

    def _notify_rules(self, device_id: str) -> None:
        self._ctx.event_bus.push(_APP_RULES_CHANGED_EVENT, json.dumps({"device_id": device_id}))
