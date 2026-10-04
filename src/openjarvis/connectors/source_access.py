"""Source ownership, reviewed sharing and per-user consumption preferences."""

from openjarvis.core.correlation import current_identity


class SourceAccess:
    def __init__(self, store, user_id="", admin=False):
        self.store, self.user_id, self.admin = store, user_id, admin

    def shared_approved(self, record):
        if record["sharing"] != "shared" or not record["approved_by"]:
            return False
        credential_id = record["config"].get("credential_id")
        if credential_id:
            from openjarvis.connectors.source_credentials import CredentialStore

            try:
                current = CredentialStore(
                    self.store.path.with_name("source_credentials.db")
                )._row(credential_id)
            except (ValueError, OSError):
                return False
            return current["revision"] == record["approved_credential_revision"]
        return True

    def manages(self, record):
        return self.admin or (
            bool(self.user_id)
            and record["owner_id"] == self.user_id
            and record["sharing"] != "shared"
        )

    def visible(self, record):
        return (
            self.admin
            or record["owner_id"] == self.user_id
            and bool(self.user_id)
            or self.shared_approved(record)
        )

    def consumes(self, record):
        return (
            bool(record["enabled"])
            and (
                record["owner_id"] == self.user_id
                and bool(self.user_id)
                or self.shared_approved(record)
            )
            and self.store.preference(self.user_id, record["id"])
        )

    def credential_allowed(self, credentials, identity):
        if not isinstance(identity, str):
            return False
        try:
            row = credentials._row(identity)
        except ValueError:
            return False
        if self.admin or bool(self.user_id) and row["owner_id"] == self.user_id:
            return True
        # Old credentials may predate owner metadata. Inherit access only when
        # every referencing source has the same proven owner; never guess from
        # an unowned source or expose a credential used by another account.
        if not row["owner_id"] and self.user_id:
            owners = {
                source["owner_id"]
                for source in self.store.list()
                if source["config"].get("credential_id") == identity
            }
            return owners == {self.user_id}
        return False


def execution_access(store):
    # Background/unidentified callers may consume approved universal sources,
    # but cannot acquire another user's personal source through a tool call.
    identity = current_identity()
    return SourceAccess(store, identity.user_id if identity else "")


def allowed_source_ids():
    from openjarvis.connectors.source_store import SourceStore

    store = SourceStore()
    access = execution_access(store)
    return {record["id"] for record in store.list() if access.consumes(record)}


def source_visibility_sql(metadata="metadata", doc_id="doc_id", source="source"):
    """Filter named-source rows before ranking/aggregation, using live ACL state."""
    ids = sorted(allowed_source_ids())
    placeholders = ",".join("?" for _ in ids) or "NULL"
    expression = (
        f"COALESCE(json_extract({metadata}, '$.source_instance_id'), "
        f"CASE WHEN {doc_id} LIKE 'source:%' THEN substr({doc_id},8,36) END)"
    )
    named_clause = f"({expression} IS NULL OR {expression} IN ({placeholders}))"
    identity = current_identity()
    if identity is not None:
        from openjarvis.connectors.source_adapters import list_adapters
        from openjarvis.server.auth_store import AuthStore

        user = AuthStore().get_user(identity.user_id) if identity.user_id else None
        if user is None or not user["is_admin"]:
            # Legacy account-wide indexes lack a proven owner. Import them into
            # a named instance (or approve an instance) before model consumption.
            legacy = sorted(
                {a["adapter_id"] for a in list_adapters()}
                | {a["adapter_id"].removesuffix("_account") for a in list_adapters()}
                | {"gmail_imap", "imap"}
            )
            legacy_marks = ",".join("?" for _ in legacy)
            named_clause += (
                f" AND ({expression} IS NOT NULL OR {source} NOT IN ({legacy_marks}))"
            )
            ids.extend(legacy)
    return named_clause, ids
