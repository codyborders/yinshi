"""Guard the HTTP client's bundled trust store against the eMudhra regression."""

import ssl

import certifi


def test_certifi_does_not_trust_compromised_emsign_root() -> None:
    """The October 2026 fix must reach installed and packaged Python environments."""
    context = ssl.create_default_context(cafile=certifi.where())
    common_names = {
        value
        for certificate in context.get_ca_certs()
        for rdn in certificate["subject"]
        for attribute, value in rdn
        if attribute == "commonName"
    }

    assert common_names
    assert "emSign ECC Root CA - G3" not in common_names
