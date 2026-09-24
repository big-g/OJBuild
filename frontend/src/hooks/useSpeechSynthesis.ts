import { useCallback, useEffect, useRef, useState } from 'react';

export type SpeechSynthesisState = 'idle' | 'speaking';
export type SpeechSynthesisResult = 'finished' | 'error' | 'cancelled';

export function useSpeechSynthesis() {
  const [state, setState] = useState<SpeechSynthesisState>('idle');
  const [available, setAvailable] = useState(false);
  const utteranceRef = useRef<SpeechSynthesisUtterance | null>(null);
  const resolveRef = useRef<((result: SpeechSynthesisResult) => void) | null>(null);

  useEffect(() => {
    const supported =
      typeof window !== 'undefined' &&
      'speechSynthesis' in window &&
      'SpeechSynthesisUtterance' in window;

    setAvailable(supported);

    return () => {
      if (supported) {
        const resolve = resolveRef.current;
        resolveRef.current = null;
        utteranceRef.current = null;
        window.speechSynthesis.cancel();
        resolve?.('cancelled');
      }
    };
  }, []);

  const stopSpeaking = useCallback(() => {
    if (!available) return;

    const resolve = resolveRef.current;
    resolveRef.current = null;
    utteranceRef.current = null;
    window.speechSynthesis.cancel();
    setState('idle');
    resolve?.('cancelled');
  }, [available]);

  const speak = useCallback(
    (text: string): Promise<SpeechSynthesisResult> => {
      if (!available || !text.trim()) {
        return Promise.resolve('error');
      }

      // Resolve any prior caller before replacing its utterance. Browsers are
      // inconsistent about whether cancel() emits onend/onerror.
      const priorResolve = resolveRef.current;
      resolveRef.current = null;
      utteranceRef.current = null;
      window.speechSynthesis.cancel();
      priorResolve?.('cancelled');

      return new Promise((resolve) => {
        const utterance = new SpeechSynthesisUtterance(text.trim());
        utteranceRef.current = utterance;
        resolveRef.current = resolve;

        const settle = (result: SpeechSynthesisResult) => {
          if (utteranceRef.current !== utterance) {
            return;
          }
          utteranceRef.current = null;
          resolveRef.current = null;
          setState('idle');
          resolve(result);
        };

        utterance.onstart = () => {
          if (utteranceRef.current === utterance) {
            setState('speaking');
          }
        };

        utterance.onend = () => settle('finished');
        utterance.onerror = () => settle('error');

        window.speechSynthesis.speak(utterance);
      });
    },
    [available],
  );

  return {
    state,
    available,
    speaking: state === 'speaking',
    speak,
    stopSpeaking,
  };
}
