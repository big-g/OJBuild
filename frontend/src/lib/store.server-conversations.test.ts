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
});
