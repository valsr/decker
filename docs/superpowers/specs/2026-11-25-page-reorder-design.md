# Page drag-reorder — design

## Intent
Pages appear as tabs in the dashboard's page strip, in the order stored on the server, but that order can only change
by deleting and re-adding pages. Dragging a tab should reorder the profile's pages.

## Goals / non-goals / anti-goals
- Goals: drag a page tab to a new position; the order is saved on the server and survives restarts; other clients
  refresh through the existing `pages_changed` event.
- Non-goals: moving a page to another profile; reordering profiles; changing the default page, the active page or any
  key assignment.
- Anti-goals: a failed or refused move leaving the strip in an order the server does not have.

## Approaches
1. **`page_move(device_id, page_id, new_index)` RPC over the existing `position` column** (chosen): the server owns
   the order, the client sends the drop target, every client refreshes from the event.
2. Client sends the full ordered id list (`pages_reorder`): robust against concurrent edits but a bigger payload and
   wider validation for a drag of one tab. Rejected.
3. Client-only order: breaks the multi-client model and loses the order on reconnect. Rejected.

## Design
- Store (`server/pages.py`): `PageStore.move_page(device_id: str, page_id: str, new_index: int) -> bool` moves the
  page within its profile's ordered list, clamps `new_index` to `[0, count-1]`, rewrites `position` for that
  profile's pages as 0..n-1 in one transaction, returns False for an unknown page. A move to the current index is a
  successful no-op that sends no change.
- Controller (`server/page_controller.py`): `move_page(device_id, page_id, new_index) -> bool` calls the store and
  `_notify(device_id)` when the order changed.
- RPC (`server/service.py`): `@exposed page_move(device_id: str, page_id: str, new_index: int) -> bool`, next to
  `page_rename`.
- Client (`widgets/page_tab_bar.py`): `self._tabs.setMovable(True)`; a new signal
  `page_move_requested = pyqtSignal(str, int)  # page_id, new index`, emitted from `QTabBar.tabMoved(from, to)` unless
  the bar is rebuilding (`_updating`). The "+" button stays outside the tab bar so tabs cannot be dragged past it.
  An open inline rename is committed or cancelled before a drag starts rather than left floating.
- Dashboard (`views/dashboard.py`): connects `page_move_requested` to a handler that calls `page_move`; whatever the
  result, it refreshes the page list from the server so the strip always shows the stored order (a refused move
  snaps back). The selected tab follows the moved page, and the active page does not change.
- Docs: CLAUDE.md (the client/UI bullet's page-strip description and the RPC list if it enumerates page RPCs) and
  the CLI needs nothing (it introspects `commands()`).

## Left to the builder
Test layout; how `QTabBar` reports `tabMoved` when the selected tab is moved; mutual exclusion with the
double-click rename edit.
