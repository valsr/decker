# Page Reorder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dragging a page tab reorders the profile's pages, saved on the server.

**Architecture:** `PageStore.move_page` rewrites `position`; `PageController.move_page` notifies; `page_move` RPC; the tab bar is movable and emits `page_move_requested`; the dashboard calls the RPC and refreshes.

**Tech Stack:** Python 3.14, SQLite, PyQt6, pytest + assertpy.

**Spec:** `docs/superpowers/specs/2026-11-25-page-reorder-design.md`

## Global Constraints

- Quality gates: `poe format`, `poe mypy`, `poe pylint` (≥ 9.0), `poe test` (coverage ≥ 85%), plus `mypy --platform win32 .` and `mypy --platform darwin .`.
- Black 120; annotate all signatures; max 5 args; methods under 50 lines; pytest + assertpy, integration-style tests (real `PageStore`).
- Reordering stays within one profile; the default page, active page and key assignments never change.
- Moving to the current index is a successful no-op that sends no `pages_changed`.

## Review Focus

- `new_index` out of range (negative, ≥ count) clamps instead of raising or corrupting positions.
- An unknown page id returns False and writes nothing.
- Pages of other profiles on the same device keep their positions.
- A refused or failed move leaves the strip showing the server's order after the refresh.
- Dragging while an inline rename is open does not lose or misapply the rename.

---

### Task 1: Server — store, controller, RPC

**Files:**
- Modify: `src/decker/server/pages.py` (`PageStore`, near `rename_page` ~line 260)
- Modify: `src/decker/server/page_controller.py` (near `rename_page` ~line 189)
- Modify: `src/decker/server/service.py` (near `page_rename` ~line 1092)
- Test: `tests/server/test_pages.py`, `tests/server/test_page_controller.py`, `tests/server/test_service.py`

**Interfaces:**
- Produces: `PageStore.move_page(device_id: str, page_id: str, new_index: int) -> bool`; `PageController.move_page(device_id: str, page_id: str, new_index: int) -> bool`; RPC `page_move(device_id: str, page_id: str, new_index: int) -> bool`.

- [ ] **Step 1: Write the failing tests.**
  - store: `test_move_page_reorders_within_the_profile` (pages A,B,C → move C to 0 → `list_pages` ids are C,A,B; move A to 2 → C,B,A); `test_move_page_clamps_the_index` (-5 → first, 99 → last); `test_move_page_unknown_page_returns_false`; `test_move_page_keeps_other_profiles_untouched` (a second profile's order is unchanged); `test_move_page_to_current_index_is_a_noop_true` (returns True, order unchanged).
  - controller: `test_move_page_notifies_only_when_the_order_changed` (a recording of `_notify`/the pushed `pages_changed` event: one event for a real move, none for a no-op move or an unknown page).
  - service: `test_page_move_rpc_reorders_and_returns_true` and a False case for an unknown page.
- [ ] **Step 2: Run** those tests. Expected: FAIL (names undefined).
- [ ] **Step 3: Implement** the three signatures above. Store: read the profile's ordered `page_id`s via `_pages`, remove and reinsert at the clamped index, update every row's `position` under the lock in one commit; return whether the page exists. The controller must be able to tell a real change from a no-op (have the store method return a distinct signal, e.g. keep the bool contract for the RPC by comparing `list_pages` before/after in the controller, or add a private `_reorder` returning `bool | None`; pick the simpler and say which in the commit).
- [ ] **Step 4: Run** gates (`poe format`, `poe mypy`, `poe pylint`, focused tests, then `poe test`). Expected: PASS.
- [ ] **Step 5: Commit** `feat: page_move RPC reorders a profile's pages`.

---

### Task 2: Client — draggable tabs and docs

**Files:**
- Modify: `src/decker/client/widgets/page_tab_bar.py` (`PageTabBar.__init__`, signals ~line 62, rebuild code guarded by `_updating`)
- Modify: `src/decker/client/views/dashboard.py` (connect near line 198; handler near `_on_rename_page`)
- Modify: `CLAUDE.md` (client/UI bullet: page strip is draggable; mention `page_move`)
- Test: `tests/client/widgets/test_page_tab_bar.py`, `tests/client/views/test_dashboard.py`

**Interfaces:**
- Consumes: RPC `page_move(device_id, page_id, new_index) -> bool` from Task 1.
- Produces: `PageTabBar.page_move_requested = pyqtSignal(str, int)`; `DashboardView._on_move_page(self, page_id: str, new_index: int) -> None`.

- [ ] **Step 1: Write the failing tests.**
  - tab bar: `test_tabs_are_movable`; `test_moving_a_tab_emits_page_move_requested` (move tab 0 to 2 with `QTabBar.moveTab`, assert one emission `(page_id_of_that_tab, 2)`); `test_rebuilding_the_tabs_does_not_emit_a_move` (call the setter that rebuilds from the server list; no `page_move_requested`); `test_move_while_renaming_commits_or_cancels_the_edit` (an open rename edit is gone after a move and no `rename_page_requested` carries a stale index).
  - dashboard: `test_move_page_calls_rpc_and_refreshes` (the connection manager mock sees `page_move` with the device id, page id and index, then a page-list refresh); `test_failed_move_still_refreshes` (RPC returns False → refresh still requested).
- [ ] **Step 2: Run** those tests. Expected: FAIL.
- [ ] **Step 3: Implement.** `setMovable(True)` on `self._tabs`; connect `tabMoved` to a private handler that looks up the page id by the new index, emits `page_move_requested(page_id, to_index)` unless `self._updating`, and first ends any inline rename; keep the "+" button outside the `QTabBar`. Dashboard handler calls `cm.call("page_move", device_id, page_id, new_index)` and then the same refresh path used after `_on_rename_page`.
- [ ] **Step 4: Run** all gates incl. both `mypy --platform` runs. Expected: PASS.
- [ ] **Step 5: Commit** `feat: drag page tabs to reorder pages`.
