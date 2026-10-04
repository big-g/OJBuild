# Account recovery

OpenJarvis usernames can be listed by the server administrator. Passwords are
stored as salted scrypt hashes and cannot be retrieved as plaintext. Reset a
forgotten password or use the same command to replace a known password.

## Recover through the login screen

Select **Forgot username or password?** on the login screen. Enter your recovery
code and choose **Find my username** to display the existing login name. To reset
the password, enter and confirm a new password, then choose **Reset password**.
A username lookup does not consume the code; a successful password reset does.

## Prepare recovery while signed in

In **Settings → Account**, your username is displayed. Enter your current password
to change your password or generate a recovery code. New passwords entered through
the UI must contain 8–1024 characters. A recovery code expires after 30 days;
generating another replaces the previous code. Save it privately when displayed:
it is not kept in browser storage and the server stores only its hash.

Changing/resetting a password revokes existing logins and recovery codes. It
preserves the user ID and conversation ownership. Sign in again on each device.

## Already locked out without a recovery code

Local administrator access is the fallback. No email recovery service is required
or assumed. On the server, first find the username and issue a code:

```bash
cd ~/.openjarvis/src
uv run jarvis auth list-users
uv run jarvis auth recovery-code
```

Enter the resulting code in the login-screen recovery form. Never share that code
in chat or with anyone who should not control the account. The local administrator
can also reset the password directly using the existing command below.

Run these commands on the Ubuntu server as the same OS account that runs
`openjarvis-api.service` (normally `big-g`), using the same `OPENJARVIS_HOME`.
Do not use `sudo` for the account commands: a different home directory can point
to a different authentication database.

```bash
cd ~/.openjarvis/src
uv run jarvis auth list-users
uv run jarvis auth reset-password
```

The reset command asks for the login username, then the new password twice with
input hidden. You can also select the account with `--username YOUR_USERNAME`.
Do not put the password in a shell command or send it in chat.

Resetting preserves the existing user ID, projects and conversations, and revokes
that user's existing authentication sessions. Log in again on browser, desktop
and Android with the same username and new password. No service restart is
required. The shared API key is separate and is not changed by a password reset.

`auth list-users --json` returns only account IDs, usernames, display names and
disabled status. It never returns password hashes or tokens. If no accounts are
listed, first verify that the command is running under the service's OS account
and data directory. If you have never created an application login, use:

```bash
uv run jarvis auth create-user
```

The reset command requires local server filesystem access; it is not an
unauthenticated network password-reset endpoint. It does not rename the username
or re-enable a disabled account.
