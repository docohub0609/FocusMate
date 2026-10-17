import { useEffect, useRef } from 'react';
import { basicSetup } from 'codemirror';
import { python } from '@codemirror/lang-python';
import { EditorState, StateEffect, StateField } from '@codemirror/state';
import { Decoration, type DecorationSet, EditorView, keymap } from '@codemirror/view';
import { indentWithTab } from '@codemirror/commands';
import { indentUnit } from '@codemirror/language';

export type CodeRange = { startLine: number; endLine: number; text: string };
const showRanges = StateEffect.define<CodeRange[]>();
const highlights = StateField.define<DecorationSet>({
  create: () => Decoration.none,
  update(current, transaction) {
    if (transaction.docChanged) return Decoration.none;
    for (const effect of transaction.effects) if (effect.is(showRanges)) {
      const lines = new Set<number>();
      for (const range of effect.value) {
        if (range.startLine < 1 || range.endLine > transaction.state.doc.lines) continue;
        const from = transaction.state.doc.line(range.startLine).from;
        const to = transaction.state.doc.line(range.endLine).to;
        if (!transaction.state.doc.sliceString(from, to).includes(range.text)) continue;
        for (let n = range.startLine; n <= range.endLine; n++) lines.add(n);
      }
      return Decoration.set([...lines].sort((a,b) => a-b).map(n =>
        Decoration.line({ class: 'cm-coach-line' }).range(transaction.state.doc.line(n).from)));
    }
    return current;
  },
  provide: field => EditorView.decorations.from(field),
});

export function PythonEditor({ value, onChange, readOnly = false, label = 'Python代码', ranges }: {
  value: string; onChange?: (value: string) => void; readOnly?: boolean; label?: string; ranges?: CodeRange[];
}) {
  const host = useRef<HTMLDivElement>(null), view = useRef<EditorView | null>(null);
  const callback = useRef(onChange); callback.current = onChange;
  const initial = useRef(value);
  useEffect(() => {
    const editor = new EditorView({ parent: host.current!, state: EditorState.create({
      doc: initial.current,
      extensions: [basicSetup, python(), highlights, indentUnit.of('    '), keymap.of([indentWithTab]),
        EditorState.readOnly.of(readOnly), EditorView.editable.of(!readOnly),
        EditorView.contentAttributes.of({ 'aria-label': label }),
        EditorView.updateListener.of(update => { if (update.docChanged) callback.current?.(update.state.doc.toString()); }),
        EditorView.theme({ '&': { fontSize: '14px', height: 'clamp(320px, 60vh, 640px)' },
          '.cm-scroller': { fontFamily: 'Consolas, monospace', overflow: 'auto', overscrollBehavior: 'contain' },
          '&.cm-focused': { outline: '2px solid #377957' } }),
      ],
    }) });
    view.current = editor;
    return () => { view.current = null; editor.destroy(); };
  }, [readOnly, label]);
  useEffect(() => {
    const editor = view.current;
    if (editor && editor.state.doc.toString() !== value)
      editor.dispatch({ changes: { from: 0, to: editor.state.doc.length, insert: value } });
  }, [value]);
  useEffect(() => {
    const editor = view.current;
    if (!editor) return;
    editor.dispatch({ effects: showRanges.of(ranges || []) });
    // Do not move the caret or touch history. Defer scrolling during IME composition.
    let timer: ReturnType<typeof setTimeout> | undefined;
    function reveal() {
      if (view.current !== editor || !ranges?.length) return;
      if (editor!.composing) { timer = setTimeout(reveal, 100); return; }
      const line = ranges[0].startLine;
      if (line >= 1 && line <= editor!.state.doc.lines)
        editor!.dispatch({ effects: EditorView.scrollIntoView(editor!.state.doc.line(line).from, { y: 'center' }) });
    }
    reveal();
    return () => clearTimeout(timer);
  }, [ranges]);
  return <div className="python-editor" ref={host} />;
}
