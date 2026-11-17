"""HTTPS for the phone streaming page.

Browsers (iPhone Safari above all) only allow camera access on secure pages: HTTPS,
or http://localhost. A cloud VM reached by its IP address over plain HTTP is NOT
secure, so the /push page would get no camera. This module starts a second, HTTPS
listener inside the same process (same camera workers, same data).

Certificate:
  - CERT_FILE + KEY_FILE env vars set -> use that certificate (e.g. Let's Encrypt).
  - otherwise a self-signed certificate is created once and saved in data/tls/.
    The phone shows a warning the first time ("This connection is not private"):
    tap "Show Details" -> "visit this website". After that the camera works.
"""
import datetime
import ipaddress
import logging
import os
import socket
import ssl
import threading

from werkzeug.serving import make_server

log = logging.getLogger("https")


def self_signed_cert(directory):
    """Create (once) a self-signed certificate valid for 10 years. Returns (cert, key) paths."""
    cert_path = os.path.join(directory, "cert.pem")
    key_path = os.path.join(directory, "key.pem")
    if os.path.exists(cert_path) and os.path.exists(key_path):
        return cert_path, key_path

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    os.makedirs(directory, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "multicam-dashboard")])
    alt_names = [x509.DNSName("localhost"), x509.DNSName(socket.gethostname()),
                 x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
        .sign(key, hashes.SHA256())
    )
    with open(key_path, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    log.info("created self-signed certificate in %s", directory)
    return cert_path, key_path


def start_https_server(app, port, data_dir="data"):
    """Serve `app` over HTTPS on `port` in a background thread (threaded: one thread per request)."""
    cert = os.environ.get("CERT_FILE")
    key = os.environ.get("KEY_FILE")
    if not (cert and key):
        cert, key = self_signed_cert(os.path.join(data_dir, "tls"))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server = make_server("0.0.0.0", port, app, threaded=True, ssl_context=context)
    threading.Thread(target=server.serve_forever, name="https", daemon=True).start()
    log.info("HTTPS listening on port %d (phone streaming page: /push)", port)
    return server
