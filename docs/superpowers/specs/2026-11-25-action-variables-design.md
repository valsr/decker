# Per-card action variables — design

## Intent
Let one binding on a card use what another binding produced, e.g. a card whose press runs a command and whose long
press quits it with `pid: {press.pid}`. Every action also reports whether it worked.

## Goals / non-goals / anti-goals
- Goals: each binding (`press`, `double_press`, `long_press`) of a key has local variables; every binding has
  `status` (`SUCCESS`/`FAILURE`); an action sets others through SDK `set_variable`; templates in a binding's
  arguments can read any binding of the same card as `{<binding>.<name>}`; anything not yet set reads as null.
- Non-goals: references across keys; persisting values; global-variable changes; a variable picker UI.
- Anti-goals: polluting the global variable list; silent typos (an undeclared name must not quietly succeed).

## Approaches
1. **Per-card store on the server** (chosen, agreed): in-memory, keyed by device, key and binding.
2. Reuse the global `VariableStore` with generated names: little code, but floods the global list and still needs the
   key context to resolve `{press.pid}`.
3. Values held in the plugin process: the server cannot expand arguments without asking a plugin, and a restart loses
   them.

## Design
- **Declaring.** An action's manifest gets `variables: [{"name", "description"}]`, the names it may set besides
  `status` (reserved, always present, cannot be declared). Names match `[a-z][a-z0-9_-]*`; duplicates are manifest
  errors.
- **Store.** `CardVariableStore` (server): `(device_id, key context) -> {binding: {name: value}}`. Created when a card
  activates, dropped when it deactivates or the key is unassigned or moved (a move keeps the context id, so values
  follow it). A gesture's variables are null until its action runs; changing a binding's action resets that
  binding's variables to null.
- **Status.** When a bound action's `run_action` finishes, the server sets that binding's `status`: `SUCCESS` if the
  action returned, `FAILURE` if it raised, timed out, was refused or the plugin failed. The returned value still
  goes to the caller exactly as today (discarded for gestures). Do Nothing never touches `status`.
- **Setting.** SDK `set_variable(name, value)` on the action call context sends a protocol event carrying the
  `run_action` request id; the server maps it to the running binding and stores the value. Names must be declared by
  that action (else an error is returned to the plugin and logged); values are JSON, at most 64 KiB encoded. Protocol
  version bumps; docs say plugins using it need Decker `>=0.9` (builder confirms the number).
- **Reading.** When a binding's arguments are resolved (the existing template pass), `{press.pid}`,
  `{double_press.status}`, `{long_press.status}` expand from the store; `press`/`double_press`/`long_press` are the
  only qualifiers. Null expands to an empty string in text, and a whole-value reference (`"{press.pid}"` alone)
  keeps the typed value (null stays `None`, numbers stay numbers). Unknown qualifier or name inside braces is left
  as-is so it cannot clash with global `{name}` variables. Resolution happens at gesture time, not when the card is
  saved, because the value changes between presses.
- **Visibility.** `variables_list` gains the card variables for a given device and key so the CLI can inspect them.
- **Editor.** Argument fields that accept templates already do; the editor gets no picker in this change.

## Left to the builder
Where the template pass lives, the exact event and error names, how a nested `call_action` inherits the running
binding (it should not set the caller's variables), Sequence's own `status`, test layout.
Docs: `docs/plugins/protocol.md`, `sdk-quickstart.md`, `package-and-manifest.md`, CLAUDE.md.
