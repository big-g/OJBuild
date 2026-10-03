"""Exact LAN authorization and real TLS handshakes without network traffic."""

import socket
import ssl
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from openjarvis.connectors import imap_sources
from openjarvis.connectors.imap_network import (
    tls_context,
    validate_lan_endpoint,
    validate_network_config,
)
from openjarvis.connectors.imap_sources import IMAPSource


def dns(addresses):
    return [
        (
            socket.AF_INET6 if ":" in value else socket.AF_INET,
            socket.SOCK_STREAM,
            6,
            "",
            (value, 993),
        )
        for value in addresses
    ]


@pytest.mark.parametrize(
    "address", ["192.168.1.20", "10.1.2.3", "172.16.0.2", "fd00::20"]
)
def test_exact_private_destination_is_resolved_and_pinned(monkeypatch, address):
    resolve = Mock(return_value=dns([address, address]))
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    assert validate_lan_endpoint("MAIL.INTERNAL", 993, address) == (
        "mail.internal",
        (address,),
    )
    resolve.assert_called_once_with(
        "mail.internal", 993, socket.AF_UNSPEC, socket.SOCK_STREAM
    )


@pytest.mark.parametrize(
    "addresses",
    [
        ["192.168.1.20", "192.168.1.21"],
        ["192.168.1.20", "93.184.216.34"],
        ["192.168.1.20", "127.0.0.1"],
        ["169.254.169.254"],
        [],
    ],
)
def test_any_unapproved_dns_result_rejects_entire_destination(monkeypatch, addresses):
    monkeypatch.setattr(socket, "getaddrinfo", Mock(return_value=dns(addresses)))
    with pytest.raises(ValueError):
        validate_lan_endpoint("mail.internal", 993, "192.168.1.20")


@pytest.mark.parametrize(
    "host",
    ["localhost", "a.localhost", "metadata.google.internal", "metadata.google.com"],
)
def test_localhost_and_metadata_names_never_resolve(monkeypatch, host):
    resolve = Mock(side_effect=AssertionError("No DNS"))
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    with pytest.raises(ValueError):
        validate_lan_endpoint(host, 993, "192.168.1.20")
    resolve.assert_not_called()


@pytest.mark.parametrize(
    "addresses",
    [
        "",
        "192.168.1.0/24",
        "localhost",
        "127.0.0.1",
        "0.0.0.0",
        "169.254.169.254",
        "224.0.0.1",
        "8.8.8.8",
        "100.64.0.1",
        "::1",
        "fe80::1",
        "fd00::1%eth0",
        "::ffff:192.168.1.20",
        ",".join(f"10.0.0.{i}" for i in range(1, 34)),
    ],
)
def test_lan_authorization_cannot_be_a_broad_or_special_destination(addresses):
    with pytest.raises(ValueError):
        validate_network_config({"network_access": "lan", "lan_addresses": addresses})


def test_public_defaults_and_canonical_exact_list():
    assert validate_network_config({}) == {
        "network_access": "public",
        "lan_addresses": "",
        "tls_trust": "system",
        "ca_certificate": "",
    }
    config = validate_network_config(
        {
            "network_access": "lan",
            "lan_addresses": "fd00:0::20\n192.168.1.20,192.168.1.20",
        }
    )
    assert config["lan_addresses"] == "192.168.1.20, fd00::20"
    with pytest.raises(ValueError):
        validate_network_config({"lan_addresses": "192.168.1.20"})


@pytest.mark.parametrize("security", ["tls", "starttls"])
def test_named_lan_transport_pins_and_rechecks_on_every_connection(
    tmp_path, monkeypatch, security
):
    source = IMAPSource(
        config={
            "host": "mail.internal",
            "network_access": "lan",
            "lan_addresses": "192.168.1.20",
            "security": security,
        },
        token_path=tmp_path / "unused",
    )
    source.deadline = time.monotonic() + 120
    source.bytes = 0
    resolve = Mock(side_effect=[dns(["192.168.1.20"]), dns(["192.168.1.21"])])
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    client = Mock(starttls=Mock(return_value=("OK", [])))
    constructor = Mock(return_value=client)
    monkeypatch.setattr(
        imap_sources, "_TLS" if security == "tls" else "_StartTLS", constructor
    )
    assert source.connect() is client
    assert constructor.call_args.args == (
        "mail.internal",
        993 if security == "tls" else 143,
        "192.168.1.20",
    )
    context = (
        constructor.call_args.kwargs.get("ssl_context")
        if security == "tls"
        else client.starttls.call_args.kwargs["ssl_context"]
    )
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    with pytest.raises(ValueError, match="outside"):
        source.connect()
    assert constructor.call_count == 1
    client.login.assert_not_called()


@pytest.fixture
def certificates():
    now = datetime.now(timezone.utc)
    root_key = ec.generate_private_key(ec.SECP256R1())
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test private CA")])
    root = (
        x509.CertificateBuilder()
        .subject_name(root_name)
        .issuer_name(root_name)
        .public_key(root_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, False, False),
            critical=True,
        )
        .sign(root_key, hashes.SHA256())
    )
    leaf = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mail.internal")])
        )
        .issuer_name(root_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("mail.internal")]), critical=False
        )
        .sign(root_key, hashes.SHA256())
    )
    return (
        root.public_bytes(serialization.Encoding.PEM).decode(),
        leaf.public_bytes(serialization.Encoding.PEM),
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


def handshake(tmp_path, config, leaf, key, hostname):
    cert_path, key_path = tmp_path / "server.pem", tmp_path / "server.key"
    cert_path.write_bytes(leaf)
    key_path.write_bytes(key)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    client_in, client_out, server_in, server_out = [ssl.MemoryBIO() for _ in range(4)]
    client = tls_context(config).wrap_bio(
        client_in, client_out, server_hostname=hostname
    )
    server = server_context.wrap_bio(server_in, server_out, server_side=True)
    completed = set()
    for _ in range(20):
        for name, peer, output, incoming in [
            ("client", client, client_out, server_in),
            ("server", server, server_out, client_in),
        ]:
            try:
                peer.do_handshake()
                completed.add(name)
            except ssl.SSLWantReadError:
                pass
            data = output.read()
            if data:
                incoming.write(data)
        if len(completed) == 2:
            return
    raise AssertionError("TLS handshake did not complete")


def test_private_ca_real_tls_verifies_root_and_hostname_and_is_source_scoped(
    tmp_path, certificates
):
    root, leaf, key = certificates
    private = validate_network_config(
        {"tls_trust": "custom_ca", "ca_certificate": root}
    )
    handshake(tmp_path, private, leaf, key, "mail.internal")
    with pytest.raises(ssl.SSLCertVerificationError):
        handshake(tmp_path, private, leaf, key, "wrong.internal")
    with pytest.raises(ssl.SSLCertVerificationError):
        handshake(tmp_path, validate_network_config({}), leaf, key, "mail.internal")


def test_ca_configuration_rejects_private_keys_leaf_certificates_and_wrong_modes(
    certificates,
):
    root, leaf, key = certificates
    for pem in [key.decode(), root + key.decode(), leaf.decode(), "", root * 33]:
        with pytest.raises(ValueError):
            validate_network_config({"tls_trust": "custom_ca", "ca_certificate": pem})
    with pytest.raises(ValueError):
        validate_network_config({"ca_certificate": root})
    with pytest.raises(ValueError):
        validate_network_config({"ca_certificate": " " * 16385})
