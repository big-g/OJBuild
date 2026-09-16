import { getBase } from './api';

const AUTH_STORAGE_KEY = 'openjarvis-auth';

export interface AuthUser {
  user_id: string;
  username: string;
  display_name: string;
}

interface StoredAuth extends AuthUser {
  sessionToken: string;
}

export function getSessionToken(): string {
  try {
    const raw = localStorage.getItem(AUTH_STORAGE_KEY);
    if (!raw) return '';

    const auth = JSON.parse(raw) as Partial<StoredAuth>;
    return typeof auth.sessionToken === 'string' ? auth.sessionToken : '';
  } catch {
    return '';
  }
}

export function getStoredUser(): AuthUser | null {
  try {
    const raw = localStorage.getItem(AUTH_STORAGE_KEY);
    if (!raw) return null;

    const auth = JSON.parse(raw) as Partial<StoredAuth>;

    if (
      typeof auth.user_id !== 'string' ||
      typeof auth.username !== 'string' ||
      typeof auth.display_name !== 'string' ||
      typeof auth.sessionToken !== 'string'
    ) {
      return null;
    }

    return {
      user_id: auth.user_id,
      username: auth.username,
      display_name: auth.display_name,
    };
  } catch {
    return null;
  }
}

function storeAuth(user: AuthUser, sessionToken: string): void {
  localStorage.setItem(
    AUTH_STORAGE_KEY,
    JSON.stringify({
      ...user,
      sessionToken,
    }),
  );
}

export function clearAuth(): void {
  localStorage.removeItem(AUTH_STORAGE_KEY);
}

export async function login(
  username: string,
  password: string,
): Promise<AuthUser> {
  const res = await fetch(`${getBase()}/v1/auth/login`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ username, password }),
  });

  if (!res.ok) {
    throw new Error(
      res.status === 401
        ? 'Invalid username or password'
        : `Login failed: ${res.status}`,
    );
  }

  const data = (await res.json()) as {
    user_id: string;
    username: string;
    display_name: string;
    session_token: string;
  };

  const user: AuthUser = {
    user_id: data.user_id,
    username: data.username,
    display_name: data.display_name,
  };

  storeAuth(user, data.session_token);
  return user;
}

export async function validateSession(): Promise<AuthUser | null> {
  const token = getSessionToken();

  if (!token) return null;

  try {
    const res = await fetch(`${getBase()}/v1/auth/me`, {
      headers: {
        'X-OpenJarvis-Session': token,
      },
    });

    if (!res.ok) {
      clearAuth();
      return null;
    }

    const user = (await res.json()) as AuthUser;

    storeAuth(user, token);
    return user;
  } catch {
    return null;
  }
}

export async function logout(): Promise<void> {
  const token = getSessionToken();

  try {
    if (token) {
      await fetch(`${getBase()}/v1/auth/logout`, {
        method: 'POST',
        headers: {
          'X-OpenJarvis-Session': token,
        },
      });
    }
  } finally {
    clearAuth();
  }
}
