/**
 * Global keyboard shortcuts — the table and the matching rule, nothing else.
 *
 * Why it exists: shortcut handling used to live inside whichever component
 * wanted a key (Ask Agent owned ⌘K), so there was no single place to see what
 * is bound, no help list, and no shared "don't fire while typing" rule. This
 * module is pure (it reads plain event-like objects) so the rules are tested
 * without a DOM.
 *
 * Invariants:
 * - Plain-key shortcuts (`?`, single letters) NEVER fire while focus is in an
 *   input, textarea, select or contenteditable element — they would eat the
 *   operator's typing.
 * - ⌘/Ctrl combos (⌘K palette, ⌘J Ask Agent) DO fire while typing: they insert
 *   no character, and "open the palette from the search box I'm in" is the
 *   whole point of the chord.
 * - IME composition never triggers anything.
 * - ⌘ and Ctrl are interchangeable so the same table works on macOS, Windows
 *   and Linux.
 */

export type ShortcutActionId =
  | 'open-command-palette'
  | 'open-ask-agent'
  | 'show-shortcuts'
  | 'new-task';

export interface ShortcutDefinition {
  action: ShortcutActionId;
  /** Display form, one entry per key cap, e.g. `['⌘/Ctrl', 'K']`. */
  keys: readonly string[];
  description: string;
  /** `KeyboardEvent.key` to match (letters lower-case). */
  key: string;
  /** Requires ⌘ (macOS) or Ctrl (elsewhere). */
  withModifier: boolean;
  /** Whether it may fire while focus is in a text field. */
  allowWhileTyping: boolean;
}

export const SHORTCUTS: readonly ShortcutDefinition[] = [
  {
    action: 'open-command-palette',
    keys: ['⌘/Ctrl', 'K'],
    description: 'Open the command palette (pages, tasks, Ask Agent)',
    key: 'k',
    withModifier: true,
    allowWhileTyping: true,
  },
  {
    action: 'open-ask-agent',
    keys: ['⌘/Ctrl', 'J'],
    description: 'Open or close the Ask Agent panel',
    key: 'j',
    withModifier: true,
    allowWhileTyping: true,
  },
  {
    action: 'show-shortcuts',
    keys: ['?'],
    description: 'Show this list of keyboard shortcuts',
    key: '?',
    withModifier: false,
    allowWhileTyping: false,
  },
  {
    action: 'new-task',
    keys: ['N'],
    description: 'Create a new task',
    key: 'n',
    withModifier: false,
    allowWhileTyping: false,
  },
];

/** The subset of `EventTarget` / `HTMLElement` the typing rule reads. */
export interface TargetLike {
  tagName?: string;
  isContentEditable?: boolean;
}

/** The subset of `KeyboardEvent` the matcher reads. */
export interface KeyEventLike {
  key: string;
  metaKey?: boolean;
  ctrlKey?: boolean;
  altKey?: boolean;
  shiftKey?: boolean;
  isComposing?: boolean;
  target?: TargetLike | null;
}

const TYPING_TAGS = new Set(['INPUT', 'TEXTAREA', 'SELECT']);

/** True when keystrokes on `target` are text entry, not commands. */
export function isTypingTarget(target: TargetLike | null | undefined): boolean {
  if (!target) return false;
  if (target.isContentEditable) return true;
  return TYPING_TAGS.has((target.tagName ?? '').toUpperCase());
}

function matchesDefinition(event: KeyEventLike, definition: ShortcutDefinition): boolean {
  const modifier = Boolean(event.metaKey || event.ctrlKey);
  if (event.altKey) return false;
  if (definition.withModifier) {
    // Shift-chords (⌘⇧K etc.) are left to the browser / other bindings.
    return modifier && !event.shiftKey && event.key.toLowerCase() === definition.key;
  }
  // `?` already implies Shift on most layouts, so Shift is not checked; an
  // upper-case letter (Shift held) does not equal the lower-case table key.
  return !modifier && event.key === definition.key;
}

/** The action `event` triggers, or null. */
export function matchShortcut(
  event: KeyEventLike,
  table: readonly ShortcutDefinition[] = SHORTCUTS,
): ShortcutActionId | null {
  // Chrome's autofill dispatches keydown events with no `key` at all.
  if (event.isComposing || typeof event.key !== 'string') return null;
  const typing = isTypingTarget(event.target);
  for (const definition of table) {
    if (typing && !definition.allowWhileTyping) continue;
    if (matchesDefinition(event, definition)) return definition.action;
  }
  return null;
}
