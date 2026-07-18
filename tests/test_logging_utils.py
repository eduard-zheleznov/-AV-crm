import logging

from avito_crm.logging_utils import RedactingFormatter


def test_logging_formatter_redacts_telegram_token_url():
    token = "123456:must-never-appear"
    record = logging.LogRecord(
        "httpx",
        logging.INFO,
        __file__,
        1,
        f'HTTP Request: POST https://api.telegram.org/bot{token}/sendMessage "200 OK"',
        (),
        None,
    )

    rendered = RedactingFormatter("%(message)s").format(record)

    assert token not in rendered
    assert "bot***REDACTED***/sendMessage" in rendered


def test_logging_formatter_redacts_token_without_full_url():
    token = "123456:must-never-appear"
    record = logging.LogRecord(
        "httpcore",
        logging.DEBUG,
        __file__,
        1,
        f"request target=/bot{token}/sendMessage",
        (),
        None,
    )

    rendered = RedactingFormatter("%(message)s").format(record)

    assert token not in rendered
