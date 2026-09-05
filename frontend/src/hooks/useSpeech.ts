import { useState, useCallback, useRef, useEffect } from 'react';
import { transcribeAudio, fetchSpeechHealth } from '../lib/api';

export type SpeechState = 'idle' | 'recording' | 'transcribing';

const SILENCE_DURATION_MS = 1500;
const VOLUME_THRESHOLD = 0.025;
const CHECK_INTERVAL_MS = 50;

//export function useSpeech() {
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
  const autoStopRef = useRef(false);

  useEffect(() => {
    fetchSpeechHealth()
      .then((health) => setAvailable(health.available))
      .catch(() => setAvailable(false));

    return () => {
      if (silenceTimerRef.current !== null) {
        window.clearInterval(silenceTimerRef.current);
      }
      audioContextRef.current?.close();
    };
  }, []);

  const stopRecording = useCallback(async (): Promise<string> => {
    return new Promise((resolve, reject) => {
      const recorder = mediaRecorderRef.current;

      if (!recorder || recorder.state !== 'recording') {
        reject(new Error('Not recording'));
        return;
      }

      if (silenceTimerRef.current !== null) {
        window.clearInterval(silenceTimerRef.current);
        silenceTimerRef.current = null;
      }

      silenceStartedRef.current = null;
      speechDetectedRef.current = false;

      recorder.onstop = async () => {
	console.log('[Speech] MediaRecorder onstop fired');
        setState('transcribing');

        streamRef.current?.getTracks().forEach((track) => track.stop());
        streamRef.current = null;

        if (audioContextRef.current) {
          await audioContextRef.current.close();
          audioContextRef.current = null;
        }

        analyserRef.current = null;

        const blob = new Blob(chunksRef.current, {
          type: recorder.mimeType || 'audio/webm',
        });

        chunksRef.current = [];

        try {

          console.log('[Speech] Sending audio:', {
            size: blob.size,
            type: blob.type,
          });
          
//          const result = await transcribeAudio(blob);
//          console.log('[Speech] Transcription returned:', result);

//          setState('idle');
//          resolve(result.text);
          const result = await transcribeAudio(blob);
          console.log('[Speech] Transcription returned:', result);

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

      console.log('[Speech] Calling MediaRecorder.stop()');
      recorder.stop();
    });
  }, []);

  const startRecording = useCallback(async (): Promise<void> => {
    setError(null);

    if (!navigator.mediaDevices?.getUserMedia) {
      setError('Microphone not supported in this browser');
      return;
    }

    try {
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
          console.log('[Speech] Silence detected; calling stopRecording()');

          silenceTimerRef.current = null;

//          stopRecording()
//            .then((text) => {
//	       console.log('[Speech] stopRecording() completed:', text);
//             })
//            .catch((err) => {
//               console.error('[Speech] stopRecording() failed:', err);
//             });

          autoStopRef.current = true;

          stopRecording()
             .then((text) => {
             console.log('[Speech] stopRecording() completed:', text);
            })
            .catch((err) => {
              console.error('[Speech] stopRecording() failed:', err);
            });
        }
      }, CHECK_INTERVAL_MS);
    } catch (err) {
      setError('Microphone access denied');
      setState('idle');
    }
  }, [stopRecording, onTranscription]);

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
