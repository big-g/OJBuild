import { describe, expect, it } from 'vitest';
import { microphoneStartError, recordingFilename } from './speech-format';

describe('recorded audio upload', () => {
  it.each([
    ['audio/webm;codecs=opus', 'recording.webm'],
    ['audio/ogg;codecs=opus', 'recording.ogg'],
    ['audio/mp4;codecs=mp4a.40.2', 'recording.m4a'],
    ['audio/wav', 'recording.wav'],
  ])('uses the actual %s container', (mimeType, filename) => {
    expect(recordingFilename(mimeType)).toBe(filename);
  });

  it('does not label an unknown recording as WebM', () => {
    expect(recordingFilename('')).toBeNull();
    expect(recordingFilename('audio/unknown')).toBeNull();
  });

  it('distinguishes denied permission from missing hardware', () => {
    expect(microphoneStartError({ name: 'NotAllowedError' })).toContain('Allow the microphone');
    expect(microphoneStartError({ name: 'NotFoundError' })).toBe('No microphone found.');
  });
});
