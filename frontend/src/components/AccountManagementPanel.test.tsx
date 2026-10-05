import { renderToStaticMarkup } from 'react-dom/server';
import { beforeEach, expect, it, vi } from 'vitest';
import { AccountManagementPanel } from './AccountManagementPanel';
import { AccountSecurityPanel } from './AccountSecurityPanel';
const { user } = vi.hoisted(() => ({ user: { user_id: 'admin', username: '<script>name</script>', display_name: 'Admin', is_admin: true } }));
vi.mock('../lib/auth', () => ({ getStoredUser: () => user, accountRequest: vi.fn(), clearAuth: vi.fn() }));
beforeEach(() => { user.is_admin = true; });
it('defaults to User and uses an unsaved password input', () => {
  const html = renderToStaticMarkup(<AccountManagementPanel />);
  expect(html).toContain('<option value="user" selected="">User</option>');
  expect(html).toContain('Administrator</option>');
  expect(html).toContain('type="password"');
  expect(html).toContain('autoComplete="new-password"');
  expect(html).toContain('Role changes sign the affected account out');
});
it('does not offer account administration to ordinary users', () => {
  user.is_admin = false;
  expect(renderToStaticMarkup(<AccountManagementPanel />)).toBe('');
});
it('shows role and escapes the account username', () => {
  const html = renderToStaticMarkup(<AccountSecurityPanel />);
  expect(html).toContain('Role: <strong>Administrator</strong>');
  expect(html).toContain('&lt;script&gt;name');
  expect(html).not.toContain('<script>');
});
