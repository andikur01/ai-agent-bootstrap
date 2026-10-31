"""Security tests: redaction must be impossible to bypass by accident."""
from __future__ import annotations

import logging
from pathlib import Path

from app.core.logging_setup import RedactionFilter, setup_logging
from app.security.redact import PLACEHOLDER, redact, redact_mapping


def test_redacts_telegram_bot_token():
    text = "token saya 123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
    out = redact(text)
    assert "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw" not in out
    assert PLACEHOLDER in out


def test_redacts_provider_keys():
    for key in (
        "sk-abcdefghijklmnopqrstuvwxyz123456",
        "gsk_abcdefghijklmnopqrstuvwxyz123456",
        "AIzaSyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q",
        "hf_abcdefghijklmnopqrstuvwxyz",
        "ghp_abcdefghijklmnopqrstuvwxyz1234",
    ):
        out = redact(f"key={key}")
        assert key not in out, key
        assert PLACEHOLDER in out


def test_redacts_bearer_and_jwt():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    out = redact(f"Authorization: Bearer {jwt}")
    assert jwt not in out
    assert redact("Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghij") != (
        "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghij"
    )


def test_redacts_private_key_block():
    text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"
    assert "MIIEowIBAAKCAQEA" not in redact(text)


def test_redacts_key_value_pairs():
    out = redact("api_key=supersecretvalue123 token: abcdef123456 password=hunter2xyz")
    assert "supersecretvalue123" not in out
    assert "abcdef123456" not in out
    assert "hunter2xyz" not in out


def test_redact_mapping_blanks_sensitive_keys():
    data = {"api_key": "secret-value", "nested": {"password": "abc"}, "safe": "hello"}
    out = redact_mapping(data)
    assert out["api_key"] == PLACEHOLDER
    assert out["nested"]["password"] == PLACEHOLDER
    assert out["safe"] == "hello"


def test_logging_filter_scrubs_records(capsys):
    setup_logging(level="INFO", json_output=False)
    logger = logging.getLogger("test.redaction")
    logger.info("calling with sk-abcdefghijklmnopqrstuvwxyz123456")
    captured = capsys.readouterr()
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in captured.out
    assert PLACEHOLDER in captured.out


def test_filter_scrubs_extra_fields():
    record = logging.LogRecord(
        name="x",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hi",
        args=(),
        exc_info=None,
    )
    record.api_key = "sk-abcdefghijklmnopqrstuvwxyz123456"
    RedactionFilter().filter(record)
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in record.api_key


def test_screenshot_secret_regions_are_blurred(tmp_path):
    """A screenshot that renders a secret must not leave with it readable."""
    from PIL import Image, ImageDraw

    from app.services.browser_qa import mask_secrets_in_image

    secret = "gsk_livesecretvalue1234567890"
    src = tmp_path / "shot.png"
    img = Image.new("RGB", (400, 300), "white")
    draw = ImageDraw.Draw(img)
    draw.text((10, 10), secret, fill="black")
    img.save(src)

    out = Path(mask_secrets_in_image(src, [secret]))
    assert out.exists() and out != str(src)
    with Image.open(out) as masked:
        strip = masked.convert("RGB").crop((0, 0, 400, 40))
        pixels = list(strip.getdata())
    assert all(pixel != (0, 0, 0) for pixel in pixels)


def test_assert_no_secrets_raises_for_known_value(settings, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_verysecretvalue1234567890")
    from app.core import config as config_module

    config_module.get_settings.cache_clear()
    from app.core.errors import SecretLeakError
    from app.security.redact import assert_no_secrets

    try:
        assert_no_secrets("here it is gsk_verysecretvalue1234567890", "test")
    except SecretLeakError:
        pass
    else:
        raise AssertionError("expected SecretLeakError")
    config_module.get_settings.cache_clear()
