import { useCallback, useEffect, useRef, useState } from 'react';

export type SpeechSynthesisState = 'idle' | 'speaking';

export function useSpeechSynthesis() {
  const [state, setState] = useState<SpeechSynthesisState>('idle');
  const [available, setAvailable] = useState(false);
  const utteranceRef = useRef<SpeechSynthesisUtterance | null>(null);

  useEffect(() => {
    const supported =
      typeof window !== 'undefined' &&
      'speechSynthesis' in window &&
      'SpeechSynthesisUtterance' in window;

    setAvailable(supported);

    return () => {
      if (supported) {
        window.speechSynthesis.cancel();
      }
    };
  }, []);

  const stopSpeaking = useCallback(() => {
    if (!available) return;

    window.speechSynthesis.cancel();
    utteranceRef.current = null;
    setState('idle');
  }, [available]);

  const speak = useCallback(
    (text: string): Promise<void> => {
      if (!available || !text.trim()) {
        return Promise.resolve();
      }

      return new Promise((resolve) => {
        window.speechSynthesis.cancel();

        const utterance = new SpeechSynthesisUtterance(text.trim());
        utteranceRef.current = utterance;

        utterance.onstart = () => {
          setState('speaking');
        };

        utterance.onend = () => {
          if (utteranceRef.current === utterance) {
            utteranceRef.current = null;
          }
          setState('idle');
          resolve();
        };

        utterance.onerror = () => {
          if (utteranceRef.current === utterance) {
            utteranceRef.current = null;
          }
          setState('idle');
          resolve();
        };

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
