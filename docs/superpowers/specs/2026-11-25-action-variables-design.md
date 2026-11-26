# Per-card action variables — design

## Intent
Let one binding on a card use what another binding produced, e.g. a card whose press runs a command and whose long
press quits it with `pid: {press.pid}`. Every action also reports whether it worked, and the steps of a Sequence
each get variables of their own, so step 2 can use what step 1 produced (`{press.step_1.pid}`).

## Goals / non-goals / anti-goals
- Goals: each binding (`press`, `double_press`, `long_press`) of a key has local variables; every binding has
  `status` (`SUCCESS`/`FAILURE`); an action sets others through SDK `set_variable`; templates in a binding's
  arguments can read any binding of the same card as `{<binding>.<name>}`; anything not yet set reads as null; a
  nested action call can be given a *slot* label and then has variables and a `status` of its own,
  `{<binding>.<slot>.<name>}`; Sequence labels its steps `step_1`..`step_10`.
- Non-goals: references across keys; persisting values across server restarts; global-variable changes; a variable
  picker UI.
- Anti-goals: polluting the global variable list; silent typos (an undeclared name must not quietly succeed).

## Approaches
1. **Per-card store on the server** (chosen, agreed): in-memory, keyed by key context, binding and slot.
2. Reuse the global `VariableStore` with generated names: little code, but floods the global list and still needs the
   key context to resolve `{press.pid}`.
3. Values held in the plugin process: the server cannot expand arguments without asking a plugin, and a restart loses
   them.

## Design
- **Declaring.** An action's manifest gets `variables: [{"name", "description"}]`, the names it may set besides
  `status` (reserved, always present, cannot be declared). Names match `[a-z][a-z0-9_-]*`; duplicates are manifest
  errors.
- **Store.** `CardVariableStore` (server): `context id -> {binding: {name: value, slot: {name: value}}}`. Values
  **survive deactivation**: switching pages or profiles, or moving the key (same context id), keeps them. They are
  dropped when the card is removed for good (unassigned, replaced by another card) and at server restart. A binding's
  variables are null until its action runs; changing a binding's *action* (not only its arguments) resets that
  binding's variables and slots to null.
- **Status.** When a bound action's `run_action` finishes, the server sets that binding's `status`: `SUCCESS` if the
  action returned, `FAILURE` if it raised, timed out, was refused or the plugin failed. The returned value still
  goes to the caller exactly as today (discarded for gestures). Do Nothing never touches `status`. A slotted nested
  call sets `<binding>.<slot>.status` the same way when it finishes.
- **Slots.** SDK `call_action(..., slot="step_1")` (protocol field `slot`) labels a nested call. The server gives the
  call the binding of the run its `parent` request belongs to, plus the slot, but only when that parent is a run of a
  gesture binding itself (one level; a slotted call cannot hand out further slots). Slots match
  `[a-z][a-z0-9_-]*`, at most 32 characters, and may not be `status`; a bad slot fails the call. A nested call without
  a slot is not attached to any binding: its `set_variable` returns an error. Sequence passes `step_N` for step N.
- **Setting.** SDK `ctx.set_variable(name, value)` on the action call context sends a protocol event carrying the
  `run_action` request id, and the plugin waits for the server's answer (so a value is stored before the action
  ends). The server maps the request to the binding (and slot) it belongs to and stores the value there. The name
  must be declared by the action that is running (else the SDK raises `ActionError` and the server logs it); values
  are JSON, at most 64 KiB encoded. Protocol version goes to 6; docs say plugins that declare action `variables` need
  Decker `>=0.8`.
- **Reading.** When an action runs, template fields of its arguments are resolved as today and now also expand
  `{press.pid}`, `{double_press.status}`, `{long_press.status}` and `{press.step_1.pid}` from the store of the card
  that the call belongs to (gesture calls, and slotted calls through their binding). Only text and text_style fields
  that opt in with `template: true` expand; a whole-value reference is therefore always text (numbers and booleans as
  their JSON text, null as an empty string). A two-part reference to a name the bound action did not declare (and is
  not `status`), or an unknown qualifier, is left as typed so it cannot clash with global `{name}` variables; a
  three-part slot reference is never left as typed (unset reads as empty). Resolution happens when the action runs,
  not when the card is saved, because values change between presses and between Sequence steps.
- **Visibility.** The CLI can list a key's card variables: new RPC `key_variables(device_id, page_id, key_index)`.
- **Editor.** Argument fields that accept templates already do; the editor gets no picker in this change. Core's HTTP
  Request `url` field opts in to templates; `body` does not, because `{{`/`}}` are template escapes and would corrupt
  saved nested JSON bodies (`{"a": {"b": 1}}`).

## Left to the builder
Where the template pass lives, exact event and error names beyond those above, test layout.
Docs: `docs/plugins/protocol.md`, `sdk-quickstart.md`, `package-and-manifest.md`, CLAUDE.md.
