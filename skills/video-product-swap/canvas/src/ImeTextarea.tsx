import { useLayoutEffect, useRef, type TextareaHTMLAttributes } from 'react';

export const isImeKey = (event: { isComposing: boolean; keyCode: number }) => event.isComposing || event.keyCode === 229;

type Props = Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, 'value' | 'defaultValue' | 'onChange'> & {
  value: string;
  onCommit: (value: string) => void;
};

// Chromium owns the DOM draft until the IME commits a candidate. In particular,
// polling/autosave renders must never write textarea.value during composition.
export function ImeTextarea({ value, onCommit, ...props }: Props) {
  const input = useRef<HTMLTextAreaElement>(null);
  const initialValue = useRef(value);
  const composing = useRef(false);
  const committed = useRef(value);
  const previousValue = useRef(value);
  useLayoutEffect(() => {
    if (!composing.current && previousValue.current !== value && input.current) {
      if (input.current.value !== value) input.current.value = value;
      committed.current = value;
    }
    previousValue.current = value;
  }, [value]);
  const commit = (next: string) => {
    if (next === committed.current) return;
    committed.current = next;
    onCommit(next);
  };
  return <textarea {...props} ref={input} defaultValue={initialValue.current}
    onCompositionStart={() => { composing.current = true; }}
    onCompositionEnd={event => { composing.current = false; commit(event.currentTarget.value); }}
    onChange={event => {
      if (!composing.current && !(event.nativeEvent as InputEvent).isComposing) commit(event.currentTarget.value);
    }}
    onBlur={event => {
      // Native close/switch can blur before Chromium delivers compositionend.
      composing.current = false;
      commit(event.currentTarget.value);
      props.onBlur?.(event);
    }} />;
}

export function blurActiveEditor() {
  const active = document.activeElement;
  if (active instanceof HTMLTextAreaElement && !active.readOnly) active.blur();
}
