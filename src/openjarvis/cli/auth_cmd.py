"""CLI commands for API key management."""

from __future__ import annotations

import os
import re
import stat

import click

import uuid

from openjarvis.server.auth_store import AuthStore
from openjarvis.sessions.session import SessionStore

from openjarvis.core.config import (
    DEFAULT_CONFIG_DIR,
    DEFAULT_CONFIG_PATH,
)
from openjarvis.server.auth_middleware import generate_api_key


@click.group("auth")
def auth() -> None:
    """Manage API authentication keys."""


@auth.command("create-key")
def create_key() -> None:
    """Generate a new API key and store it in config."""
    key = generate_api_key()
    config_path = DEFAULT_CONFIG_PATH

    # Ensure config directory exists
    DEFAULT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # Read existing config or start fresh
    if config_path.exists():
        content = config_path.read_text()
    else:
        content = ""

    # Update or add [server.auth] section
    if "[server.auth]" in content:
        content = re.sub(
            r'(api_key\s*=\s*)"[^"]*"',
            f'\\1"{key}"',
            content,
        )
    else:
        content += f'\n[server.auth]\napi_key = "{key}"\n'

    config_path.write_text(content)
    os.chmod(config_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600

    click.echo(f"API key generated: {key}")
    click.echo(f"Stored in: {config_path}")
    click.echo("File permissions set to 0600 (user-only read/write).")


@auth.command("revoke-key")
def revoke_key() -> None:
    """Revoke the current API key."""
    config_path = DEFAULT_CONFIG_PATH
    if not config_path.exists():
        click.echo("No config file found.")
        return

    content = config_path.read_text()
    if "api_key" not in content:
        click.echo("No API key found in config.")
        return

    content = re.sub(r'api_key\s*=\s*"[^"]*"', 'api_key = ""', content)
    config_path.write_text(content)
    click.echo("API key revoked.")

@auth.command("create-user")
@click.option("--username", prompt=True, help="Login username.")
@click.option("--display-name", prompt=True, help="User's display name.")
def create_user(username: str, display_name: str) -> None:
    """Create a local OpenJarvis user account."""
    username = username.strip()
    display_name = display_name.strip()

    if not username:
        raise click.ClickException("Username cannot be empty.")

    if not display_name:
        raise click.ClickException("Display name cannot be empty.")

    password = click.prompt(
        "Password",
        hide_input=True,
        confirmation_prompt=True,
    )

    # Generate the application user ID independently of the login username.
    user_id = f"user_{uuid.uuid4().hex}"

    auth_store = AuthStore()

    if auth_store.get_user_by_username(username) is not None:
        raise click.ClickException(
            f"Username '{username}' already exists."
        )

    try:
        auth_store.create_user(
            user_id=user_id,
            username=username,
            display_name=display_name,
            password=password,
        )
    except Exception as exc:
        raise click.ClickException(
            f"Failed to create authentication account: {exc}"
        ) from exc

    # Bridge the authentication identity into the existing
    # OpenJarvis session/user system.
    session_store = SessionStore()
    session_store.ensure_user(
        user_id=user_id,
        display_name=display_name,
    )

    click.echo()
    click.echo("OpenJarvis user created successfully.")
    click.echo(f"User ID:      {user_id}")
    click.echo(f"Username:     {username}")
    click.echo(f"Display name: {display_name}")

@auth.command("reset-password")
@click.option("--username", prompt=True, help="Login username.")
def reset_password(username: str) -> None:
    """Reset the password for an existing OpenJarvis user."""
    username = username.strip()

    if not username:
        raise click.ClickException("Username cannot be empty.")

    auth_store = AuthStore()
    user = auth_store.get_user_by_username(username)

    if user is None:
        raise click.ClickException(
            f"Username '{username}' does not exist."
        )

    password = click.prompt(
        "New password",
        hide_input=True,
        confirmation_prompt=True,
    )

    try:
        auth_store.set_password(
            user_id=str(user["user_id"]),
            password=password,
        )
    except Exception as exc:
        raise click.ClickException(
            f"Failed to reset password: {exc}"
        ) from exc

    click.echo()
    click.echo("Password reset successfully.")
    click.echo(f"Username: {username}")
    click.echo("Existing authentication sessions were revoked.")
