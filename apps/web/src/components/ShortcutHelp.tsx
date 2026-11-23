'use client';

/**
 * The `?` modal: every global keyboard shortcut, rendered from the one table
 * in `lib/shortcuts.ts` so the help can never list a binding that does not
 * exist (or miss one that does).
 */

import React from 'react';
import { CModal, CModalBody, CModalHeader, CModalTitle } from '@coreui/react-pro';
import { SHORTCUTS } from '@/lib/shortcuts';

export function ShortcutHelp({ visible, onClose }: { visible: boolean; onClose: () => void }) {
  return (
    <CModal visible={visible} onClose={onClose} alignment="center" aria-labelledby="shortcut-help-title">
      <CModalHeader>
        <CModalTitle id="shortcut-help-title">Keyboard shortcuts</CModalTitle>
      </CModalHeader>
      <CModalBody>
        <table className="table table-sm mb-2">
          <tbody>
            {SHORTCUTS.map((shortcut) => (
              <tr key={shortcut.action}>
                <td className="text-nowrap" style={{ width: '1%' }}>
                  {shortcut.keys.map((cap, index) => (
                    <React.Fragment key={cap}>
                      {index > 0 && <span className="mx-1 text-body-secondary">+</span>}
                      <kbd>{cap}</kbd>
                    </React.Fragment>
                  ))}
                </td>
                <td>{shortcut.description}</td>
              </tr>
            ))}
            <tr>
              <td className="text-nowrap">
                <kbd>Esc</kbd>
              </td>
              <td>Close the open dialog or panel</td>
            </tr>
          </tbody>
        </table>
        <div className="small text-body-secondary">
          Single-key shortcuts are ignored while you are typing in a field; ⌘/Ctrl
          chords work everywhere.
        </div>
      </CModalBody>
    </CModal>
  );
}
