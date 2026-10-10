import { beforeEach, expect, it, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { AdministratorSettingsPanel, parseAdministratorValue } from './AdministratorSettingsPanel';

const user = { is_admin: true };
vi.mock('../lib/auth', () => ({ getStoredUser: () => user }));
vi.mock('../lib/store', () => ({ useAppStore: { getState: vi.fn() } }));
beforeEach(() => { user.is_admin = true; });
it('limits shared configuration controls to administrators', () => {
  user.is_admin = false;
  expect(renderToStaticMarkup(<AdministratorSettingsPanel />)).toBe('');
});
it('explains model swapping, persistence, restart requirements and secret handling', () => {
  const html = renderToStaticMarkup(<AdministratorSettingsPanel />);
  expect(html).toContain('Server default model');
  expect(html).toContain('Memory extraction model');
  expect(html).toContain('Chat model during local generation');
  expect(html).toContain('Automatic smaller model in the same family');
  expect(html).toContain('Follow server default');
  expect(html).toContain('saved in the database');
  expect(html).toContain('server restart');
  expect(html).toContain('never displays saved secrets');
  expect(html).toContain('Find a parameter');
});
it('rejects blank/nonfinite/fractional integer inputs and executable JSON objects', () => {
  const field = { key: 'memory.max_facts', editable: true, type: 'int' as const };
  for (const value of ['', 'Infinity', '3.4']) expect(() => parseAdministratorValue(field, value)).toThrow();
  expect(parseAdministratorValue(field, '10')).toBe(10);
  const list = { ...field, type: 'list' as const };
  expect(parseAdministratorValue(list, '["one", "two"]')).toEqual(['one', 'two']);
  expect(() => parseAdministratorValue(list, '[{"command":"run"}]')).toThrow();
});
