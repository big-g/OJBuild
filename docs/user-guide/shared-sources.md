# Personal and universal data sources

Named connections on the Data Sources page have a database-backed owner and
sharing state. Creating a source makes it personal to the signed-in account.
A user can request universal access; a designated administrator reviews the
connection and chooses **Approve for everyone**. A pending request is not
visible to other accounts. Administrators may also create and approve a universal
connection directly, such as a Weather provider for the household.

## Designate the administrator

After pulling the update, run this from `~/.openjarvis/src`, as the service's
normal OS user, without `sudo`:

```bash
uv run jarvis auth list-users
uv run jarvis auth set-admin --username YOUR_USERNAME
```

Refresh the web app. New accounts are ordinary users by default; existing
accounts are not silently promoted. To remove the role, add `--revoke` to the
same command. The server checks the current role on every operation, so role
revocation does not require signing out all devices. The master server API key
remains a privileged server-administration credential; do not distribute it to
ordinary users, who should use their human login.

## Choose sources for your account

Every account sees its personal connections and enabled or disabled approved
universal connections. **Use this source for my account** controls consumption
without changing anyone else's selection. Multiple connections for the same
purpose can coexist: enable a preferred Weather connection and disable the
others for your account, or create a personal alternative. An administrator's
global Disable control makes a source unavailable to every consumer.

Only administrators manage an approved universal connection's configuration,
credentials, authorization, schedules and sync. Consumers see its name, provider,
sharing state and selection control; they do not receive credential references,
configuration values, connection status details, job history or audit records.
Owners continue to manage their personal/pending sources. Changing an approved
source's configuration or account authorization returns it to pending review.
Rotating a credential used by an approved source also withdraws approval before
the rotation. A pending connection can still be used by its owner.

Sharing changes and configuration edits retain the existing revision and audit
checks. Shared selection does not grant tool capabilities: existing capability,
trust, bounded execution and evidence policies still apply. Named-source live
reads, keyword/hybrid retrieval and SQL aggregation obey the same per-account
selection. Changes affect subsequent reads; they cannot retract content already
sent in a conversation. Disabling a source for an account does not delete its
indexed documents.

## Existing sources

The migration recovers an owner only from a recorded source-creation/import audit
actor. Connections without a proven owner remain administrator-only for review.
They are not automatically made universal. When an administrator changes their
sharing state, an unowned connection is assigned to that administrator.
Indexes, checkpoints, encrypted credentials and source IDs are retained.

Legacy connector controls/imports are administrator-only. Legacy provider indexes
without named-source ownership are not used for ordinary authenticated accounts;
import the saved connection into a named instance and sync it, then keep it
personal or explicitly approve it. This avoids guessing who owns old account
credentials or exposing an old Gmail/Slack index to newly created users.

## Verify

Create a personal Weather connection under one account; another account should
not see it. Request sharing; the second account should still not see it until an
administrator approves. The second account should then see a read-only universal
card and be able to toggle its own use preference. Edit the approved connection
as administrator and verify that it becomes pending and disappears for other
accounts until reviewed again. Check a personal alternative in the same way.
