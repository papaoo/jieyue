import os
from pathlib import Path
import subprocess
import sys

import pytest

from src.services.research_delivery.credentials import CredentialVault, CredentialError, validate_webhook_url


URL = "https://open.feishu.cn/open-apis/bot/v2/hook/fake-test-token"


@pytest.mark.parametrize("url", ["http://open.feishu.cn/open-apis/bot/v2/hook/x", "https://evil.example/hook/x", "https://open.feishu.cn@evil.example/hook/x", "https://open.feishu.cn:444/open-apis/bot/v2/hook/x", URL + "?redirect=x", URL + "#fragment", "https://open.feishu.cn/open-apis/bot/v2/hook/a/b", "https://open.feishu.cn/open-apis/bot/v2/hook/%2F", URL + "?", URL + "#", URL + "\n"])
def test_webhook_allowlist(url):
    with pytest.raises(CredentialError) as error:
        validate_webhook_url(url)
    assert url not in str(error.value)


def test_vault_immutable_restart_and_no_plaintext_windows(tmp_path):
    vault = CredentialVault(tmp_path / "vault")
    first = vault.store(URL, "fake-sensitive-secret")
    second = vault.store(URL, "rotated-fake-secret")
    assert first != second
    assert CredentialVault(tmp_path / "vault").resolve(first).signing_secret == "fake-sensitive-secret"
    assert vault.resolve(second).signing_secret == "rotated-fake-secret"
    if os.name == "nt":
        assert all(b"fake-sensitive-secret" not in path.read_bytes() for path in (tmp_path / "vault").iterdir())
    else:
        assert (tmp_path / "vault").stat().st_mode & 0o777 == 0o700
        assert all(path.stat().st_mode & 0o777 == 0o600 for path in (tmp_path / "vault").iterdir())
    with pytest.raises(CredentialError):
        vault.resolve("../../outside")


def test_windows_protection_failure_closed(tmp_path, monkeypatch):
    vault = CredentialVault(tmp_path / "vault")
    monkeypatch.setattr(vault, "_protect", lambda data: (_ for _ in ()).throw(RuntimeError(URL)))
    with pytest.raises(CredentialError) as error:
        vault.store(URL, "fake-sensitive-secret")
    assert URL not in str(error.value)
    assert list((tmp_path / "vault").iterdir()) == []


def test_noncanonical_scheme_rejected_by_validator_fingerprint_and_vault(tmp_path):
    from src.services.research_delivery.credentials import credential_fingerprint

    uppercase = URL.replace("https://", "HTTPS://", 1)
    vault = CredentialVault(tmp_path / "vault")
    for operation in (validate_webhook_url, credential_fingerprint, vault.store):
        with pytest.raises(CredentialError) as error:
            operation(uppercase)
        assert uppercase not in str(error.value)
    assert not (tmp_path / "vault").exists()


def test_imports_do_not_initialize_config_or_database(tmp_path):
    code = """
import src.config
import sqlalchemy
def forbidden(*args, **kwargs):
    raise AssertionError('Unexpected startup I/O')
src.config.setup_env = forbidden
src.config.get_config = forbidden
sqlalchemy.create_engine = forbidden
import src.repositories.research_delivery_models
import src.repositories.research_delivery_repository
import src.services.research_delivery.credentials
import src.services.research_delivery.subscriptions
"""
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1]), "ENV_FILE": str(tmp_path / "not-present.env"), "DATABASE_PATH": str(tmp_path / "not-present.db")}
    completed = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=45)
    assert completed.returncode == 0, completed.stderr
    assert list(tmp_path.iterdir()) == []
