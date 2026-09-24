import type { SpeechSynthesisResult } from '../hooks/useSpeechSynthesis';

export interface ConversationModeTransition {
  stopVoiceLoop: boolean;
  resetStopping: boolean;
  abortGeneration: boolean;
}

export function getConversationModeTransition(
  wasEnabled: boolean,
  enabled: boolean,
): ConversationModeTransition {
  return {
    stopVoiceLoop: wasEnabled && !enabled,
    resetStopping: !wasEnabled && enabled,
    // Conversation Mode controls audio interaction only. The model response
    // remains a normal chat response and must be allowed to finish as text.
    abortGeneration: false,
  };
}

export function isVoiceInteraction(messageText: string | undefined): boolean {
  return messageText !== undefined;
}

export function shouldResumeListening(args: {
  conversationMode: boolean;
  voiceInteraction: boolean;
  stopping: boolean;
  speechResult: SpeechSynthesisResult;
}): boolean {
  return (
    args.conversationMode &&
    args.voiceInteraction &&
    !args.stopping &&
    args.speechResult !== 'cancelled'
  );
}
