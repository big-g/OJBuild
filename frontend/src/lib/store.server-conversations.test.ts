import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

class MemoryStorage {
  private store = new Map<string, string>();

  getItem(key: string): string | null {
    return this.store.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    this.store.set(key, String(value));
  }

  removeItem(key: string): void {
    this.store.delete(key);
  }
}

const api = vi.hoisted(() => ({
  createProject: vi.fn(),
  createSession: vi.fn(),
  deleteSession: vi.fn(),
  fetchProjects: vi.fn(),
  fetchSessions: vi.fn(),
  fetchSession: vi.fn(),
  importSessionMessages: vi.fn(),
}));

vi.mock('./api', () => api);

beforeEach(() => {
  vi.resetModules();
  vi.clearAllMocks();
  (globalThis as unknown as { localStorage: MemoryStorage }).localStorage =
    new MemoryStorage();
});

afterEach(() => {
  (globalThis as unknown as { localStorage?: MemoryStorage }).localStorage =
    undefined;
});

describe('server conversation continuity', () => {
  it('restores server sessions and active conversation history', async () => {
    api.fetchSessions.mockResolvedValue([
      {
        session_id: 'shared-1',
        user_id: 'user-1',
        project_id: 'project-1',
        project_name: 'Default',
        title: 'Server conversation',
        channel_ids: {},
        created_at: 100,
        last_activity: 200,
        metadata: {},
      },
    ]);
    api.fetchSession.mockResolvedValue({
      session_id: 'shared-1',
      title: 'Server conversation',
      last_activity: 200,
      messages: [
        { role: 'user', content: 'Hello from another device', timestamp: 150 },
        { role: 'assistant', content: 'Welcome back', timestamp: 151 },
        { role: 'system', content: 'internal summary', timestamp: 152 },
      ],
    });

    const { useAppStore } = await import('./store');
    await useAppStore.getState().syncServerConversations();

    const state = useAppStore.getState();
    expect(state.activeId).toBe('session-shared-1');
    expect(state.conversations).toHaveLength(1);
    expect(state.messages.map((message) => message.content)).toEqual([
      'Hello from another device',
      'Welcome back',
    ]);
    expect(state.conversations[0].sessionId).toBe('shared-1');
  });

  it('loads the latest server history when a session is selected', async () => {
    api.fetchSessions.mockResolvedValue([
      {
        session_id: 'shared-2',
        user_id: 'user-1',
        project_id: 'project-1',
        project_name: 'Default',
        title: 'Shared',
        channel_ids: {},
        created_at: 100,
        last_activity: 200,
        metadata: {},
      },
    ]);
    api.fetchSession.mockResolvedValue({
      session_id: 'shared-2',
      title: 'Shared',
      last_activity: 201,
      messages: [
        { role: 'user', content: 'Updated remotely', timestamp: 201 },
      ],
    });

    const { useAppStore } = await import('./store');
    await useAppStore.getState().syncServerConversations();
    const conversationId = useAppStore.getState().activeId!;
    useAppStore.getState().selectConversation(conversationId);
    await Promise.resolve();
    await Promise.resolve();

    expect(useAppStore.getState().messages[0].content).toBe('Updated remotely');
  });

  it('deletes a server session when its conversation is removed', async () => {
    api.fetchSessions.mockResolvedValue([
      {
        session_id: 'shared-3',
        user_id: 'user-1',
        project_id: 'project-1',
        project_name: 'Default',
        title: 'Shared',
        channel_ids: {},
        created_at: 100,
        last_activity: 200,
        metadata: {},
      },
    ]);
    api.fetchSession.mockResolvedValue({
      session_id: 'shared-3',
      title: 'Shared',
      last_activity: 200,
      messages: [],
    });
    api.deleteSession.mockResolvedValue(undefined);

    const { useAppStore } = await import('./store');
    await useAppStore.getState().syncServerConversations();
    useAppStore.getState().deleteConversation('session-shared-3');

    expect(api.deleteSession).toHaveBeenCalledWith('shared-3');
    expect(useAppStore.getState().conversations).toHaveLength(0);
  });

  it('migrates local conversations with their rich message metadata', async () => {
    const localConversation = {
      id: 'local-1',
      title: 'Old research chat',
      createdAt: 100_000,
      updatedAt: 200_000,
      model: 'test-model',
      messages: [
        {
          id: 'message-1',
          role: 'assistant',
          content: 'Research result',
          timestamp: 200_000,
          isResearch: true,
          toolCalls: [
            {
              id: 'tool-1',
              tool: 'web_search',
              arguments: '{}',
              status: 'success',
              result: 'Found sources',
            },
          ],
          researchSources: [{ ref: 1, title: 'Source', url: 'https://example.org' }],
        },
      ],
    };
    localStorage.setItem(
      'openjarvis-conversations',
      JSON.stringify({
        version: 1,
        conversations: { 'local-1': localConversation },
        activeId: 'local-1',
      }),
    );
    api.fetchSessions.mockResolvedValue([]);
    api.fetchProjects.mockResolvedValue([
      { project_id: 'project-1', name: 'Default' },
    ]);
    api.createSession.mockResolvedValue({
      session_id: 'migrated-1',
      title: 'Old research chat',
    });
    api.importSessionMessages.mockResolvedValue(undefined);
    api.fetchSession.mockResolvedValue({
      session_id: 'migrated-1',
      title: 'Old research chat',
      last_activity: 200,
      messages: [
        {
          role: 'assistant',
          content: 'Research result',
          timestamp: 200,
          metadata: {
            local_message_id: 'message-1',
            isResearch: true,
            toolCalls: localConversation.messages[0].toolCalls,
            researchSources: localConversation.messages[0].researchSources,
          },
        },
      ],
    });

    const { useAppStore } = await import('./store');
    await useAppStore.getState().syncServerConversations();

    expect(api.importSessionMessages).toHaveBeenCalledWith(
      'migrated-1',
      [
        expect.objectContaining({
          role: 'assistant',
          content: 'Research result',
          metadata: expect.objectContaining({
            local_message_id: 'message-1',
            isResearch: true,
            toolCalls: localConversation.messages[0].toolCalls,
            researchSources: localConversation.messages[0].researchSources,
          }),
        }),
      ],
    );
    expect(useAppStore.getState().conversations[0].sessionId).toBe('migrated-1');
    expect(useAppStore.getState().messages[0].toolCalls?.[0].result).toBe(
      'Found sources',
    );
    expect(useAppStore.getState().messages[0].researchSources?.[0].url).toBe(
      'https://example.org',
    );
  });

  it('retries an incomplete migration without creating a duplicate session', async () => {
    localStorage.setItem(
      'openjarvis-conversations',
      JSON.stringify({
        version: 1,
        conversations: {
          'local-pending': {
            id: 'local-pending',
            title: 'Pending import',
            createdAt: 100_000,
            updatedAt: 200_000,
            model: 'test-model',
            messages: [
              {
                id: 'user-1',
                role: 'user',
                content: 'Keep this message',
                timestamp: 200_000,
              },
            ],
          },
        },
        activeId: 'local-pending',
      }),
    );
    const pendingSession = {
      session_id: 'pending-session',
      user_id: 'user-1',
      project_id: 'project-1',
      project_name: 'Default',
      title: 'Pending import',
      channel_ids: {},
      created_at: 100,
      last_activity: 200,
      metadata: {
        migrated_from_local: true,
        local_history_imported: false,
        local_conversation_id: 'local-pending',
      },
    };
    api.fetchSessions
      .mockResolvedValueOnce([pendingSession])
      .mockResolvedValueOnce([
        { ...pendingSession, metadata: { ...pendingSession.metadata, local_history_imported: true } },
      ]);
    api.fetchProjects.mockResolvedValue([
      { project_id: 'project-1', name: 'Default' },
    ]);
    api.importSessionMessages
      .mockRejectedValueOnce(new Error('temporary network error'))
      .mockResolvedValueOnce(undefined);
    api.fetchSession.mockResolvedValue({
      session_id: 'pending-session',
      title: 'Pending import',
      last_activity: 200,
      messages: [
        {
          role: 'user',
          content: 'Keep this message',
          timestamp: 200,
          metadata: { local_message_id: 'user-1' },
        },
      ],
    });

    const { useAppStore } = await import('./store');
    await useAppStore.getState().syncServerConversations();
    expect(useAppStore.getState().conversations[0].sessionId).toBeUndefined();

    await useAppStore.getState().syncServerConversations();

    expect(api.createSession).not.toHaveBeenCalled();
    expect(api.deleteSession).not.toHaveBeenCalled();
    expect(useAppStore.getState().conversations[0].sessionId).toBe(
      'pending-session',
    );
  });

  it('creates a server session for an active local chat before its first send', async () => {
    api.fetchProjects.mockResolvedValue([
      { project_id: 'project-1', name: 'Default' },
    ]);
    api.createSession.mockResolvedValue({
      session_id: 'new-server-session',
      title: 'New chat',
    });
    api.importSessionMessages.mockResolvedValue(undefined);

    const { useAppStore } = await import('./store');
    const conversationId = useAppStore.getState().createConversation('test-model');
    const sessionId = await useAppStore
      .getState()
      .ensureServerConversation(conversationId);

    expect(sessionId).toBe('new-server-session');
    expect(api.importSessionMessages).toHaveBeenCalledWith(
      'new-server-session',
      [],
    );
    expect(
      useAppStore
        .getState()
        .conversations.find((conversation) => conversation.id === conversationId)
        ?.sessionId,
    ).toBe('new-server-session');
  });
});
