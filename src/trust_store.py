import os
import ssl
from pathlib import Path
from typing import List, Optional

import certifi

from src.app_identity import app_data_dir

# Environment variables curl_cffi, the HTTP and websocket client, reads its CA bundle path from.
CA_BUNDLE_ENV_VARS = ('SSL_CERT_FILE', 'CURL_CA_BUNDLE', 'REQUESTS_CA_BUNDLE')

# Extended key usage a Windows store certificate must be trusted for to verify HTTPS servers.
_SERVER_AUTH_OID = '1.3.6.1.5.5.7.3.1'


def _default_bundle_path() -> Path:
    """
    Returns:
        Path: Per-user location of the generated CA bundle.
    """
    return app_data_dir() / 'ca-bundle.pem'


def _windows_store_pems() -> List[str]:
    """
    Collects the certificates Windows trusts for server authentication,
    including CAs deployed by an organization (e.g. a firewall's TLS
    inspection CA).

    Returns:
        List[str]: PEM-encoded certificates from the ROOT and CA system stores.
    """
    pems = []
    seen = set()
    for store in ('ROOT', 'CA'):
        for der, encoding, trust in ssl.enum_certificates(store):
            if encoding != 'x509_asn' or der in seen:
                continue
            if trust is True or _SERVER_AUTH_OID in trust:
                seen.add(der)
                pems.append(ssl.DER_cert_to_PEM_cert(der))
    return pems


def use_windows_trust_store(bundle_path: Optional[Path] = None) -> Optional[Path]:
    """
    Makes curl_cffi trust the certificates Windows trusts, for this process only.

    curl_cffi verifies HTTPS against certifi's bundle alone, so behind a
    TLS-inspecting proxy (a corporate firewall re-signing traffic with its
    own CA installed in Windows) every market data request and stream fails
    with "unable to get local issuer certificate". This writes certifi's
    bundle plus the Windows store certificates to one file and points
    curl_cffi at it through CURL_CA_BUNDLE. Must run before the first
    request, as curl_cffi reads the variable when its session is created.

    Args:
        bundle_path (Optional[Path]): Where to write the bundle; defaults to
            a per-user application data folder.

    Returns:
        Optional[Path]: The bundle now in use, or None when skipped: not on
            Windows, a CA bundle is already configured through the
            environment, or the bundle couldn't be written (certifi's
            bundle is then used as before).
    """
    if not hasattr(ssl, 'enum_certificates'):
        return None
    if any(os.environ.get(name) for name in CA_BUNDLE_ENV_VARS):
        return None

    bundle_path = bundle_path or _default_bundle_path()
    # Write-then-rename so a process reading the bundle never sees half of it
    tmp_path = bundle_path.with_name(f'{bundle_path.name}.{os.getpid()}.tmp')
    try:
        with open(certifi.where(), encoding='utf-8') as file:
            certifi_pems = file.read()
        contents = certifi_pems.rstrip('\n') + '\n' + ''.join(_windows_store_pems())

        bundle_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(contents, encoding='utf-8')
        os.replace(tmp_path, bundle_path)
    except OSError:
        # Windows can't replace a file another process has open, e.g. an
        # antivirus scan; the bundle written last time trusts the same CAs
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        if not bundle_path.exists():
            return None

    os.environ['CURL_CA_BUNDLE'] = str(bundle_path)
    return bundle_path
