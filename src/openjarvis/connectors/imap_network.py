"""Per-source LAN destinations and certificate trust; no global network bypass."""

import ipaddress
import re
import socket
import ssl

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding

from openjarvis.connectors.imap import _BLOCKED_HOSTNAMES, _normalize_hostname

_LAN_RANGES = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
)


def _lan_address(value):
    if not isinstance(value, str) or "%" in value:
        raise ValueError("Use exact private LAN IP addresses")
    address = ipaddress.ip_address(value)
    if not any(address in network for network in _LAN_RANGES):
        raise ValueError("Only RFC1918 or IPv6 unique-local addresses are allowed")
    return str(address)


def validate_network_config(config):
    mode = config.get("network_access", "public")
    raw = config.get("lan_addresses", "")
    if (
        not isinstance(mode, str)
        or mode not in {"public", "lan"}
        or not isinstance(raw, str)
        or len(raw) > 2048
    ):
        raise ValueError("Invalid IMAP destination policy")
    addresses = re.split(r"[\s,]+", raw.strip()) if raw.strip() else []
    if mode == "public" and addresses:
        raise ValueError("Public mode cannot authorize LAN addresses")
    if mode == "lan" and not 1 <= len(addresses) <= 32:
        raise ValueError("Authorize 1–32 exact private LAN IP addresses")
    addresses = sorted({_lan_address(value) for value in addresses})
    trust = config.get("tls_trust", "system")
    pem = config.get("ca_certificate", "")
    if (
        not isinstance(trust, str)
        or trust not in {"system", "custom_ca"}
        or not isinstance(pem, str)
        or len(pem) > 16384
    ):
        raise ValueError("Invalid IMAP certificate trust")
    if trust == "system":
        if pem.strip():
            raise ValueError("Select private CA trust to use a CA certificate")
        pem = ""
    else:
        if not re.fullmatch(
            r"(?:\s*-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+"
            r"-----END CERTIFICATE-----\s*){1,8}",
            pem,
        ):
            raise ValueError("Provide only PEM CA certificates, up to 16 KiB")
        try:
            certificates = x509.load_pem_x509_certificates(pem.encode("ascii"))
            if not certificates or any(
                not certificate.extensions.get_extension_for_class(
                    x509.BasicConstraints
                ).value.ca
                for certificate in certificates
            ):
                raise ValueError
            pem = "".join(
                certificate.public_bytes(Encoding.PEM).decode()
                for certificate in certificates
            )
            ssl.create_default_context(cadata=pem)
        except (
            ValueError,
            x509.ExtensionNotFound,
            x509.DuplicateExtension,
            UnicodeError,
            ssl.SSLError,
        ):
            raise ValueError("Invalid CA certificate bundle") from None
    return {
        "network_access": mode,
        "lan_addresses": ", ".join(addresses),
        "tls_trust": trust,
        "ca_certificate": pem,
    }


def validate_lan_endpoint(host, port, addresses):
    """Every resolved IP must be an explicitly configured private address."""
    normalized = _normalize_hostname(host)
    if normalized in _BLOCKED_HOSTNAMES or normalized.endswith(".localhost"):
        raise ValueError("Localhost and metadata IMAP hosts are not allowed")
    allowed = {_lan_address(value.strip()) for value in addresses.split(",")}
    try:
        resolved = socket.getaddrinfo(
            normalized, port, socket.AF_UNSPEC, socket.SOCK_STREAM
        )
    except socket.gaierror:
        raise ValueError("Unable to resolve authorized LAN mail host") from None
    if not resolved or len(resolved) > 64:
        raise ValueError("Invalid LAN mail host resolution")
    pinned = []
    for _, _, _, _, sockaddr in resolved:
        address = _lan_address(sockaddr[0])
        if address not in allowed:
            raise ValueError("Mail host resolved outside its authorized LAN addresses")
        if address not in pinned:
            pinned.append(address)
    return normalized, tuple(pinned)


def tls_context(config):
    # With cadata, Python loads these roots instead of the system trust store.
    return ssl.create_default_context(
        **(
            {"cadata": config["ca_certificate"]}
            if config["tls_trust"] == "custom_ca"
            else {}
        )
    )
