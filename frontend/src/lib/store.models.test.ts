import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ModelInfo } from '../types';

class MemoryStorage {
  private store = new Map<string, string>();

  getItem(key: string): string | null {
    return this.store.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    this.store.set(key, String(value));
  }
}

const model = (id: string): ModelInfo => ({
  id,
  object: 'model',
  created: 0,
  owned_by: 'openjarvis',
});

beforeEach(() => {
  vi.resetModules();
  (globalThis as unknown as { localStorage: MemoryStorage }).localStorage =
    new MemoryStorage();
});

afterEach(() => {
  (globalThis as unknown as { localStorage?: MemoryStorage }).localStorage =
    undefined;
});

describe('setModels', () => {
  it('uses the provider manifest instead of name heuristics for configured models', async () => {
    const { useAppStore } = await import('./store');
    const selected = 'oj/0123456789abcdef0123456789abcdef/nomic-embed-text';
    useAppStore.getState().setModels([{ ...model(selected), owned_by: 'configured_ollama', capabilities: ['completion'] }]);
    expect(useAppStore.getState().selectedModel).toBe(selected);
    useAppStore.getState().setModels([model('qwen3.5:9b')]);
    expect(useAppStore.getState().selectedModel).toBe(selected);
  });
  it('keeps a configured-server selection when that server disappears', async () => {
    const { useAppStore } = await import('./store');
    const selected = 'oj/0123456789abcdef0123456789abcdef/qwen3.5%3A9b';
    useAppStore.getState().setSelectedModel(selected);
    useAppStore.getState().setModels([model('qwen3.5:9b')]);
    expect(useAppStore.getState().selectedModel).toBe(selected);
  });
  it('does not select an embedding-only model', async () => {
    const { useAppStore } = await import('./store');

    useAppStore.getState().setModels([model('nomic-embed-text')]);

    expect(useAppStore.getState().selectedModel).toBe('');
  });

  it('clears a missing selection when no chat fallback exists', async () => {
    const { useAppStore } = await import('./store');
    useAppStore.getState().setSelectedModel('deleted-chat-model');

    useAppStore.getState().setModels([model('nomic-embed-text')]);

    expect(useAppStore.getState().selectedModel).toBe('');
  });

  it('replaces an embedding selection with an available chat model', async () => {
    const { useAppStore } = await import('./store');
    useAppStore.getState().setSelectedModel('all-minilm:latest');

    useAppStore.getState().setModels([
      model('all-minilm:latest'),
      model('qwen3.5:4b'),
    ]);

    expect(useAppStore.getState().selectedModel).toBe('qwen3.5:4b');
  });
});

it('uses the administrator default for an unset selection and preserves an explicit choice', async () => {
  const { useAppStore } = await import('./store');
  const models = [model('qwen3.5:9b'), { ...model('ornith-1.5:35b'), is_default: true }];
  useAppStore.getState().setModels(models);
  expect(useAppStore.getState().selectedModel).toBe('ornith-1.5:35b');
  useAppStore.getState().setSelectedModel('qwen3.5:9b');
  useAppStore.getState().setModels(models);
  expect(useAppStore.getState().selectedModel).toBe('qwen3.5:9b');
});
