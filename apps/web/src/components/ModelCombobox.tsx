/**
 * ModelCombobox — the one model input every form uses.
 *
 * A free-text field with an autocomplete list of the vendor's models from the
 * worker's agent registry. Focusing the field shows every available model;
 * typing ("claude…", "opus", "latest") narrows the list. Any id may still be
 * typed by hand — a pinned/dated model, or one that has since been deprecated
 * and is therefore no longer suggested.
 *
 * A native `<datalist>` is not used because it filters by the current value:
 * a field prefilled with the default model would only ever suggest itself.
 *
 * Keyboard: ↓/↑ move through the list, Enter picks, Escape closes.
 */
'use client';

import React, { useEffect, useId, useMemo, useRef, useState } from 'react';
import { CFormInput } from '@coreui/react-pro';
import { pickerModels, type RegistryModel } from '@/lib/agent-vendors';
import { useAgentRegistry } from '@/lib/use-agent-registry';

interface ModelComboboxProps {
  value: string;
  onChange: (value: string) => void;
  /** Vendor whose models are suggested. Defaults to Anthropic (repo default model). */
  vendor?: string;
  /** Optional name attribute so the input can be embedded in a form. */
  name?: string;
  placeholder?: string;
  disabled?: boolean;
}

const NO_ACTIVE_OPTION = -1;

/** Models whose id or label contains every whitespace-separated term of `query`. */
function filterModels(models: RegistryModel[], query: string): RegistryModel[] {
  const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (terms.length === 0) return models;
  return models.filter((model) => {
    const haystack = `${model.id} ${model.label}`.toLowerCase();
    return terms.every((term) => haystack.includes(term));
  });
}

export function ModelCombobox({
  value,
  onChange,
  vendor = 'anthropic',
  name,
  placeholder,
  disabled,
}: ModelComboboxProps) {
  const registry = useAgentRegistry();
  const listId = useId();
  const listRef = useRef<HTMLUListElement>(null);

  const [open, setOpen] = useState(false);
  // The list is narrowed only once the user types. Until then the field may
  // hold a stored or default model, and the whole list should be on offer.
  const [isFiltering, setIsFiltering] = useState(false);
  const [activeIndex, setActiveIndex] = useState(NO_ACTIVE_OPTION);

  const models = useMemo(() => pickerModels(registry, vendor), [registry, vendor]);
  const options = useMemo(
    () => (isFiltering ? filterModels(models, value) : models),
    [models, value, isFiltering],
  );
  const isListVisible = open && !disabled && options.length > 0;
  const activeOption = isListVisible ? options[activeIndex] : undefined;
  const optionId = (index: number) => `${listId}-option-${index}`;

  // Keep the keyboard-highlighted option in view inside the scrolling list.
  useEffect(() => {
    if (!isListVisible || activeIndex === NO_ACTIVE_OPTION) return;
    const element = listRef.current?.children[activeIndex] as HTMLElement | undefined;
    element?.scrollIntoView({ block: 'nearest' });
  }, [isListVisible, activeIndex]);

  const showAllModels = () => {
    setOpen(true);
    setIsFiltering(false);
    setActiveIndex(NO_ACTIVE_OPTION);
  };

  const close = () => {
    setOpen(false);
    setIsFiltering(false);
    setActiveIndex(NO_ACTIVE_OPTION);
  };

  const select = (model: RegistryModel) => {
    onChange(model.id);
    close();
  };

  const handleInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    onChange(e.target.value);
    setOpen(true);
    setIsFiltering(true);
    setActiveIndex(NO_ACTIVE_OPTION);
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    switch (e.key) {
      case 'ArrowDown':
      case 'ArrowUp': {
        e.preventDefault();
        if (!isListVisible) {
          setOpen(true);
          return;
        }
        const step = e.key === 'ArrowDown' ? 1 : -1;
        // From "nothing highlighted", ↓ lands on the first option and ↑ on the last.
        const from = activeIndex === NO_ACTIVE_OPTION && step === -1 ? 0 : activeIndex;
        setActiveIndex((from + step + options.length) % options.length);
        return;
      }
      case 'Enter':
        // Only swallow Enter when it picks an option; otherwise the form submits as usual.
        if (activeOption) {
          e.preventDefault();
          select(activeOption);
        }
        return;
      case 'Escape':
        if (isListVisible) {
          e.preventDefault();
          e.stopPropagation(); // do not also close a surrounding modal
          close();
        }
        return;
      default:
    }
  };

  return (
    <div className="position-relative">
      <CFormInput
        name={name}
        value={value}
        onChange={handleInputChange}
        onFocus={showAllModels}
        onClick={() => setOpen(true)}
        onBlur={close}
        onKeyDown={handleKeyDown}
        placeholder={placeholder ?? 'Type or select a model…'}
        autoComplete="off"
        disabled={disabled}
        role="combobox"
        aria-autocomplete="list"
        aria-expanded={isListVisible}
        aria-controls={listId}
        aria-activedescendant={activeOption ? optionId(activeIndex) : undefined}
      />
      {isListVisible && (
        <ul
          id={listId}
          ref={listRef}
          role="listbox"
          className="dropdown-menu show w-100 mt-1 shadow-sm"
          style={{ maxHeight: 280, overflowY: 'auto' }}
        >
          {options.map((model, index) => (
            <li
              key={model.id}
              id={optionId(index)}
              role="option"
              aria-selected={model.id === value}
              className={`dropdown-item d-flex flex-column${index === activeIndex ? ' active' : ''}`}
              style={{ cursor: 'pointer', whiteSpace: 'normal' }}
              // mousedown, not click: keep focus in the input so blur does not
              // close the list before the pick registers.
              onMouseDown={(e) => {
                e.preventDefault();
                select(model);
              }}
              onMouseEnter={() => setActiveIndex(index)}
            >
              <span className={model.id === value ? 'fw-semibold' : undefined}>{model.id}</span>
              {model.label !== model.id && (
                <small className={index === activeIndex ? undefined : 'text-body-secondary'}>
                  {model.label}
                  {model.is_default && ' · default'}
                </small>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
