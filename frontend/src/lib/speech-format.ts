/** Match the upload extension to the container selected by MediaRecorder. */
export function recordingFilename(mimeType: string): string | null {
  const container = mimeType.split(';', 1)[0].trim().toLowerCase();
  switch (container) {
    case 'audio/webm':
    case 'video/webm':
      return 'recording.webm';
    case 'audio/ogg':
      return 'recording.ogg';
    case 'audio/mp4':
      return 'recording.m4a';
    case 'audio/wav':
    case 'audio/wave':
    case 'audio/x-wav':
      return 'recording.wav';
    case 'audio/mpeg':
    case 'audio/mp3':
      return 'recording.mp3';
    default:
      return null;
  }
}

export function microphoneStartError(error: unknown): string {
  const name = error && typeof error === 'object' && 'name' in error
    ? String(error.name)
    : '';
  if (name === 'NotAllowedError' || name === 'PermissionDeniedError') {
    return 'Microphone access denied. Allow the microphone for this site in your browser.';
  }
  if (name === 'NotFoundError' || name === 'DevicesNotFoundError') {
    return 'No microphone found.';
  }
  if (name === 'NotReadableError') {
    return 'Microphone is in use or unavailable.';
  }
  if (name === 'SecurityError') {
    return 'Microphone access is blocked by this browser.';
  }
  return error instanceof Error
    ? `Could not start microphone: ${error.message}`
    : 'Could not start microphone.';
}
