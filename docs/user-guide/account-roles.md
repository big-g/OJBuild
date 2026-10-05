# Account roles

Every account has an explicit **User** or **Administrator** role. New accounts
are Users by default, including the first account. Existing accounts keep their
current role; updating the application never silently promotes anyone.

Administrators can create accounts from **Settings → Account management**, choose
their role, change another account's role, and remove accounts. The account list
shows each role. Changing a role revokes all of that account's login sessions;
the person must sign in again. Administrators cannot demote or remove themselves
through the web interface. Account removal deletes login/recovery credentials;
historical chats, sources and files remain associated with the original account
ID and are not transferred to a newly created account with the same username.

For an installation without an administrator, use trusted local server access:

```bash
cd ~/.openjarvis/src
uv run jarvis auth set-admin --username YOUR_OPENJARVIS_USERNAME
```

Replace the placeholder with your OpenJarvis login username, then sign out and
sign back in. To create an account locally, select the role explicitly:

```bash
uv run jarvis auth create-user --role user
uv run jarvis auth create-user --role administrator
```

Passwords are prompted privately with confirmation. `--role` defaults to `user`.
Local administration commands remain available for recovery by the server owner.

Users retain their own conversations, project organization, generated files,
password/recovery settings and named data sources. Universal sources still require
administrator approval. Server-wide model installation/removal, credentials,
legacy connectors, tools/MCP governance, global agents/tasks, approvals, skills,
memory administration, optimization and budget changes require a live human
administrator session. Shared agent events and traces are also restricted.
An API key by itself does not grant these administrator privileges, even when
human authentication was previously optional. Unclassified API mutations default
to administrator-only; new personal endpoints must be explicitly classified and
implement their own identity/ownership checks.

The backend verifies current database roles on every protected request. Hiding a
menu or changing browser storage cannot grant administrator access. Privileged
operating-system/code execution isolation remains a separate roadmap objective.
