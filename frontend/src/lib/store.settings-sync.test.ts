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

beforeEach(() => {
  vi.resetModules();
  (globalThis as unknown as { localStorage: MemoryStorage }).localStorage =
    new MemoryStorage();
});

afterEach(() => {
  (globalThis as unknown as { localStorage?: MemoryStorage }).localStorage =
    undefined;
});

describe('settings storage synchronization', () => {
  it('reloads conversation mode after another tab changes stored settings', async () => {
    const { syncSettingsFromStorage, useAppStore } = await import('./store');

    expect(useAppStore.getState().settings.conversationMode).toBe(false);

    localStorage.setItem(
      'openjarvis-settings',
      JSON.stringify({ conversationMode: true }),
    );

    syncSettingsFromStorage();

    expect(useAppStore.getState().settings.conversationMode).toBe(true);
  });
});
