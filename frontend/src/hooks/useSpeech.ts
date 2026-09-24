import { useState, useCallback, useRef, useEffect } from 'react';
import { transcribeAudio, fetchSpeechHealth } from '../lib/api';

export type SpeechState = 'idle' | 'recording' | 'transcribing';

const SILENCE_DURATION_MS = 1500;
const VOLUME_THRESHOLD = 0.025;
const CHECK_INTERVAL_MS = 50;

interface StopRecordingOptions {
  discard?: boolean;
}

export function useSpeech(onTranscription?: (text: string) => void) {
  const onTranscriptionRef = useRef(onTranscription);

  useEffect(() => {
    onTranscriptionRef.current = onTranscription;
  }, [onTranscription]);

  const [state, setState] = useState<SpeechState>('idle');
  const [error, setError] = useState<string | null>(null);
  const [available, setAvailable] = useState(false);

  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const streamRef = useRef<MediaStream | null>(null);

  const audioContextRef = useRef<AudioContext | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const silenceTimerRef = useRef<number | null>(null);
  const speechDetectedRef = useRef(false);
  const silenceStartedRef = useRef<number | null>(null);
  const discardRecordingRef = useRef(false);
  const startingRef = useRef(false);

  useEffect(() => {
    fetchSpeechHealth()
      .then((health) => setAvailable(health.available))
      .catch(() => setAvailable(false));

    return () => {
      if (silenceTimerRef.current !== null) {
        window.clearInterval(silenceTimerRef.current);
        silenceTimerRef.current = null;
      }

      const recorder = mediaRecorderRef.current;
      if (recorder?.state === 'recording') {
        recorder.onstop = null;
        recorder.stop();
      }
      mediaRecorderRef.current = null;

      streamRef.current?.getTracks().forEach((track) => track.stop());
      streamRef.current = null;

      if (audioContextRef.current) {
        void audioContextRef.current.close();
        audioContextRef.current = null;
      }
      analyserRef.current = null;
    };
  }, []);

  const stopRecording = useCallback(
    async (options: StopRecordingOptions = {}): Promise<string> => {
      return new Promise((resolve, reject) => {
        const recorder = mediaRecorderRef.current;

        if (!recorder || recorder.state !== 'recording') {
          reject(new Error('Not recording'));
          return;
        }

        discardRecordingRef.current = !!options.discard;

        if (silenceTimerRef.current !== null) {
          window.clearInterval(silenceTimerRef.current);
          silenceTimerRef.current = null;
        }

        silenceStartedRef.current = null;
        speechDetectedRef.current = false;

        recorder.onstop = async () => {
          setState(options.discard ? 'idle' : 'transcribing');

          streamRef.current?.getTracks().forEach((track) => track.stop());
          streamRef.current = null;

          if (audioContextRef.current) {
            await audioContextRef.current.close();
            audioContextRef.current = null;
          }

          analyserRef.current = null;
          mediaRecorderRef.current = null;

          const blob = new Blob(chunksRef.current, {
            type: recorder.mimeType || 'audio/webm',
          });
          chunksRef.current = [];

          if (discardRecordingRef.current) {
            discardRecordingRef.current = false;
            resolve('');
            return;
          }

          try {
            const result = await transcribeAudio(blob);
            setState('idle');

            if (result.text) {
              onTranscriptionRef.current?.(result.text);
            }

            resolve(result.text);
          } catch (err) {
            setState('idle');
            const msg =
              err instanceof Error ? err.message : 'Transcription failed';
            setError(msg);
            reject(err);
          }
        };

        recorder.stop();
      });
    },
    [],
  );

  const startRecording = useCallback(async (): Promise<void> => {
    setError(null);

    if (
      startingRef.current ||
      mediaRecorderRef.current?.state === 'recording'
    ) {
      return;
    }

    if (!navigator.mediaDevices?.getUserMedia) {
      setError('Microphone not supported in this browser');
      return;
    }

    try {
      startingRef.current = true;
      discardRecordingRef.current = false;
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: true,
      });

      streamRef.current = stream;

      const recorder = new MediaRecorder(stream);
      chunksRef.current = [];

      recorder.ondataavailable = (e) => {
        if (e.data.size > 0) {
          chunksRef.current.push(e.data);
        }
      };

      const audioContext = new AudioContext();
      const source = audioContext.createMediaStreamSource(stream);
      const analyser = audioContext.createAnalyser();

      analyser.fftSize = 1024;
      source.connect(analyser);

      audioContextRef.current = audioContext;
      analyserRef.current = analyser;

      recorder.start();
      mediaRecorderRef.current = recorder;
      setState('recording');

      const data = new Uint8Array(analyser.fftSize);

      silenceTimerRef.current = window.setInterval(() => {
        if (
          !mediaRecorderRef.current ||
          mediaRecorderRef.current.state !== 'recording'
        ) {
          return;
        }

        analyser.getByteTimeDomainData(data);

        let sum = 0;

        for (let i = 0; i < data.length; i++) {
          const normalized = (data[i] - 128) / 128;
          sum += normalized * normalized;
        }

        const rms = Math.sqrt(sum / data.length);

        if (rms >= VOLUME_THRESHOLD) {
          speechDetectedRef.current = true;
          silenceStartedRef.current = null;
          return;
        }

        if (!speechDetectedRef.current) {
          return;
        }

        if (silenceStartedRef.current === null) {
          silenceStartedRef.current = Date.now();
          return;
        }

        const silenceDuration =
          Date.now() - silenceStartedRef.current;

        if (silenceDuration >= SILENCE_DURATION_MS) {
          if (silenceTimerRef.current !== null) {
            window.clearInterval(silenceTimerRef.current);
            silenceTimerRef.current = null;
          }

          void stopRecording().catch((err) => {
            console.error('[Speech] automatic stop failed:', err);
          });
        }
      }, CHECK_INTERVAL_MS);
    } catch (err) {
      streamRef.current?.getTracks().forEach((track) => track.stop());
      streamRef.current = null;
      mediaRecorderRef.current = null;
      setError('Microphone access denied');
      setState('idle');
    } finally {
      startingRef.current = false;
    }
  }, [stopRecording]);

  return {
    state,
    error,
    available,
    startRecording,
    stopRecording,
    isRecording: state === 'recording',
    isTranscribing: state === 'transcribing',
  };
}
