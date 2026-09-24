import { describe, expect, it } from 'vitest';

import {
  getConversationModeTransition,
  isVoiceInteraction,
  shouldResumeListening,
} from './conversation-mode';

describe('conversation mode state', () => {
  it('keeps typed messages text-only', () => {
    expect(isVoiceInteraction(undefined)).toBe(false);
    expect(isVoiceInteraction('spoken transcript')).toBe(true);
  });

  it('stops the voice loop without aborting an in-flight model response', () => {
    expect(getConversationModeTransition(true, false)).toEqual({
      stopVoiceLoop: true,
      resetStopping: false,
      abortGeneration: false,
    });
  });

  it('does not restart listening after deliberate speech cancellation', () => {
    expect(
      shouldResumeListening({
        conversationMode: true,
        voiceInteraction: true,
        stopping: false,
        speechResult: 'cancelled',
      }),
    ).toBe(false);
  });

  it.each(['finished', 'error'] as const)(
    'resumes listening after %s while the voice loop remains active',
    (speechResult) => {
      expect(
        shouldResumeListening({
          conversationMode: true,
          voiceInteraction: true,
          stopping: false,
          speechResult,
        }),
      ).toBe(true);
    },
  );

  it('does not resume when conversation mode has been stopped', () => {
    expect(
      shouldResumeListening({
        conversationMode: false,
        voiceInteraction: true,
        stopping: true,
        speechResult: 'finished',
      }),
    ).toBe(false);
  });
});
