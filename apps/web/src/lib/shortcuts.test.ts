import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { SHORTCUTS, isTypingTarget, matchShortcut } from './shortcuts';

const INPUT = { tagName: 'INPUT' };
const TEXTAREA = { tagName: 'textarea' };
const SELECT = { tagName: 'SELECT' };
const EDITABLE_DIV = { tagName: 'DIV', isContentEditable: true };
const BUTTON = { tagName: 'BUTTON' };

describe('isTypingTarget', () => {
  // Feature: F-NAV-02
  it('is true for input, textarea, select and contenteditable', () => {
    for (const target of [INPUT, TEXTAREA, SELECT, EDITABLE_DIV]) {
      assert.equal(isTypingTarget(target), true, JSON.stringify(target));
    }
  });

  // Feature: F-NAV-02
  it('is false for other elements and for no target', () => {
    assert.equal(isTypingTarget(BUTTON), false);
    assert.equal(isTypingTarget({ tagName: 'BODY' }), false);
    assert.equal(isTypingTarget(null), false);
    assert.equal(isTypingTarget(undefined), false);
  });
});

describe('matchShortcut', () => {
  // Feature: F-NAV-03
  it('maps ⌘K and Ctrl+K to the command palette', () => {
    assert.equal(matchShortcut({ key: 'k', metaKey: true }), 'open-command-palette');
    assert.equal(matchShortcut({ key: 'K', ctrlKey: true }), 'open-command-palette');
  });

  // Feature: F-NAV-03
  it('maps ⌘J and Ctrl+J to Ask Agent', () => {
    assert.equal(matchShortcut({ key: 'j', metaKey: true }), 'open-ask-agent');
    assert.equal(matchShortcut({ key: 'j', ctrlKey: true }), 'open-ask-agent');
  });

  // Feature: F-NAV-03
  it('lets the ⌘ chords fire while typing, since they insert no text', () => {
    assert.equal(matchShortcut({ key: 'k', metaKey: true, target: INPUT }), 'open-command-palette');
    assert.equal(matchShortcut({ key: 'j', ctrlKey: true, target: TEXTAREA }), 'open-ask-agent');
  });

  // Feature: F-NAV-03
  it('ignores plain K/J and shifted or alt chords', () => {
    assert.equal(matchShortcut({ key: 'k' }), null);
    assert.equal(matchShortcut({ key: 'j' }), null);
    assert.equal(matchShortcut({ key: 'k', metaKey: true, shiftKey: true }), null);
    assert.equal(matchShortcut({ key: 'k', metaKey: true, altKey: true }), null);
  });

  // Feature: F-NAV-04
  it('maps ? to the shortcut list', () => {
    assert.equal(matchShortcut({ key: '?', shiftKey: true, target: BUTTON }), 'show-shortcuts');
  });

  // Feature: F-NAV-02
  it('never fires ? or single-letter shortcuts while typing', () => {
    for (const target of [INPUT, TEXTAREA, SELECT, EDITABLE_DIV]) {
      assert.equal(matchShortcut({ key: '?', shiftKey: true, target }), null);
      assert.equal(matchShortcut({ key: 'n', target }), null);
    }
    assert.equal(matchShortcut({ key: 'n', target: BUTTON }), 'new-task');
  });

  // Feature: F-NAV-02
  it('ignores IME composition, keyless events and upper-case letters', () => {
    assert.equal(matchShortcut({ key: 'k', metaKey: true, isComposing: true }), null);
    assert.equal(matchShortcut({ key: undefined as unknown as string }), null);
    assert.equal(matchShortcut({ key: 'N', shiftKey: true }), null);
  });

  // Feature: F-NAV-04
  it('lists every action exactly once in the help table', () => {
    const actions = SHORTCUTS.map((shortcut) => shortcut.action);
    assert.equal(new Set(actions).size, actions.length);
    for (const shortcut of SHORTCUTS) assert.ok(shortcut.description && shortcut.keys.length > 0);
  });
});
