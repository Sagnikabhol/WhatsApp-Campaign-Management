"""
Unit and integration tests for WhatsApp Business Cloud API integration.
Tests:
- Credential masking and redaction
- Template payload construction and E.164 phone normalization
- Test mode simulation and database MessageLog persistence
- Successful response handling and WAMID storage
- Error handling: AuthError (401), RateLimitError (429), PolicyError, TemplateError
- Retry logic on transient failures (5xx, timeouts)
- Rate limiter execution
- Bulk campaign confirmation guard
- Marketing consent enforcement
"""

import json
from unittest.mock import patch, MagicMock
import pytest
import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from database.connection import create_db_engine, init_db
from database.models import Contact, MessageLog, MessageStatusEvent, Campaign
from services.contact_service import add_or_update_contact, record_opt_out
from services.whatsapp_api import (
    WhatsAppCloudAPI,
    mask_token,
    sanitize_headers_for_logging,
    format_meta_recipient_phone,
    build_template_payload,
    WhatsAppAuthError,
    WhatsAppRateLimitError,
    WhatsAppPolicyError,
    WhatsAppTemplateError,
    WhatsAppAPIError,
    ConfirmationRequiredError,
    TokenBucketRateLimiter,
)


@pytest.fixture
def api_session():
    """Provides a fresh isolated in-memory test database session."""
    engine = create_db_engine("sqlite:///:memory:")
    init_db(engine)
    with Session(bind=engine) as session:
        yield session
    engine.dispose()


# ==============================================================================
# 1. SECURITY & TOKEN MASKING TESTS
# ==============================================================================

def test_mask_token():
    assert mask_token("") == "[NOT_CONFIGURED]"
    assert mask_token(None) == "[NOT_CONFIGURED]"
    assert mask_token("short") == "****"
    
    long_token = "EAAGm0PX4ZCpsBAK7ZCZB3exampletoken123456789"
    masked = mask_token(long_token)
    assert masked.startswith("EAAG...")
    assert masked.endswith("6789")
    assert "exampletoken" not in masked
    assert "******" in masked


def test_sanitize_headers_for_logging():
    headers = {
        "Authorization": "Bearer EAAG123456789SECRET",
        "Content-Type": "application/json",
    }
    sanitized = sanitize_headers_for_logging(headers)
    assert sanitized["Authorization"] == "Bearer [REDACTED]"
    assert sanitized["Content-Type"] == "application/json"


# ==============================================================================
# 2. PHONE NORMALIZATION & PAYLOAD BUILDER TESTS
# ==============================================================================

def test_format_meta_recipient_phone():
    assert format_meta_recipient_phone("+14155552671") == "14155552671"
    assert format_meta_recipient_phone("+919876543210") == "919876543210"
    assert format_meta_recipient_phone("14155552671") == "14155552671"


def test_build_template_payload():
    payload = build_template_payload(
        to_phone="+14155552671",
        template_name="hello_world",
        language_code="en_US",
    )
    assert payload["messaging_product"] == "whatsapp"
    assert payload["to"] == "14155552671"
    assert payload["type"] == "template"
    assert payload["template"]["name"] == "hello_world"
    assert payload["template"]["language"]["code"] == "en_US"
    assert "components" not in payload["template"]

    # Template with body parameters
    custom_payload = build_template_payload(
        to_phone="+919876543210",
        template_name="order_update",
        language_code="en",
        body_parameters=["John", "#10842"],
    )
    components = custom_payload["template"]["components"]
    assert len(components) == 1
    assert components[0]["type"] == "body"
    assert components[0]["parameters"][0]["text"] == "John"
    assert components[0]["parameters"][1]["text"] == "#10842"


# ==============================================================================
# 3. TEST MODE SIMULATION & DATABASE PERSISTENCE TESTS
# ==============================================================================

def test_default_mode_is_test():
    api = WhatsAppCloudAPI(api_token="", phone_number_id="")
    assert api.mode == "test"
    assert api.is_production is False


def test_send_template_message_test_mode_persists_to_db(api_session):
    contact = add_or_update_contact(
        phone_number="+14155552671",
        name="Authorized Tester",
        is_opted_in=True,
        db_session=api_session,
    )
    api_session.commit()

    api = WhatsAppCloudAPI(mode="test")
    res = api.send_template_message(
        to_phone=contact.phone_number,
        template_name="hello_world",
        contact_id=contact.id,
        db_session=api_session,
    )
    api_session.commit()

    assert res["status"] == "sent"
    assert res["wamid"].startswith("wamid.HBgM_SIMULATED_")
    assert res["phone_number"] == "+14155552671"

    # Verify stored in SQLite MessageLog
    log = api_session.execute(
        select(MessageLog).where(MessageLog.whatsapp_message_id == res["wamid"])
    ).scalar_one_or_none()
    assert log is not None
    assert log.status == "sent"
    assert log.contact_id == contact.id
    assert len(log.status_events) == 1
    assert log.status_events[0].status == "sent"


# ==============================================================================
# 4. LIVE API CALL HANDLING & ERROR RESPONSES
# ==============================================================================

@patch("requests.post")
def test_send_template_message_successful_api_response(mock_post, api_session):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "messaging_product": "whatsapp",
        "contacts": [{"input": "14155552671", "wa_id": "14155552671"}],
        "messages": [{"id": "wamid.HBgM_REAL_META_12345"}],
    }
    mock_post.return_value = mock_response

    contact = add_or_update_contact(
        phone_number="+14155552671",
        is_opted_in=True,
        db_session=api_session,
    )
    api_session.commit()

    api = WhatsAppCloudAPI(
        api_token="valid_test_token_12345",
        phone_number_id="100998877665544",
        mode="production",
    )

    res = api.send_template_message(
        to_phone="+14155552671",
        template_name="sample_shipping_confirmation",
        contact_id=contact.id,
        db_session=api_session,
    )
    api_session.commit()

    assert res["status"] == "sent"
    assert res["wamid"] == "wamid.HBgM_REAL_META_12345"

    log = api_session.execute(
        select(MessageLog).where(MessageLog.whatsapp_message_id == "wamid.HBgM_REAL_META_12345")
    ).scalar_one_or_none()
    assert log is not None
    assert log.phone_number == "+14155552671"


@patch("requests.post")
def test_auth_error_handling(mock_post, api_session):
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.json.return_value = {
        "error": {
            "message": "Invalid OAuth access token.",
            "type": "OAuthException",
            "code": 190,
        }
    }
    mock_post.return_value = mock_resp

    contact = add_or_update_contact(phone_number="+14155552671", is_opted_in=True, db_session=api_session)
    api_session.commit()

    api = WhatsAppCloudAPI(api_token="bad_token", phone_number_id="123", mode="production")
    with pytest.raises(WhatsAppAuthError) as exc_info:
        api.send_template_message(to_phone=contact.phone_number, template_name="hello_world", db_session=api_session)
    assert "authentication failed" in str(exc_info.value).lower()


@patch("requests.post")
def test_rate_limit_error_handling(mock_post, api_session):
    mock_resp = MagicMock()
    mock_resp.status_code = 429
    mock_resp.headers = {"Retry-After": "1"}
    mock_resp.json.return_value = {
        "error": {
            "message": "Too many requests to Meta API",
            "type": "OAuthException",
            "code": 80007,
        }
    }
    mock_post.return_value = mock_resp

    contact = add_or_update_contact(phone_number="+14155552671", is_opted_in=True, db_session=api_session)
    api_session.commit()

    api = WhatsAppCloudAPI(api_token="token", phone_number_id="123", mode="production", max_retries=1)
    with pytest.raises(WhatsAppRateLimitError):
        api.send_template_message(to_phone=contact.phone_number, template_name="hello_world", db_session=api_session)


@patch("requests.post")
def test_template_error_handling(mock_post, api_session):
    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.json.return_value = {
        "error": {
            "message": "Template does not exist in specified language",
            "type": "OAuthException",
            "code": 132001,
        }
    }
    mock_post.return_value = mock_resp

    contact = add_or_update_contact(phone_number="+14155552671", is_opted_in=True, db_session=api_session)
    api_session.commit()

    api = WhatsAppCloudAPI(api_token="token", phone_number_id="123", mode="production")
    with pytest.raises(WhatsAppTemplateError):
        api.send_template_message(to_phone=contact.phone_number, template_name="nonexistent_template", db_session=api_session)


# ==============================================================================
# 5. RETRY LOGIC ON TRANSIENT 5XX
# ==============================================================================

@patch("requests.post")
def test_retry_on_transient_500_server_error(mock_post, api_session):
    # First call: 500 error; Second call: 200 OK
    resp_fail = MagicMock()
    resp_fail.status_code = 500
    resp_fail.json.return_value = {"error": {"message": "Internal Meta Server Error", "code": 1}}

    resp_ok = MagicMock()
    resp_ok.status_code = 200
    resp_ok.json.return_value = {
        "messaging_product": "whatsapp",
        "contacts": [{"input": "14155552671", "wa_id": "14155552671"}],
        "messages": [{"id": "wamid.HBgM_RETRY_SUCCESS"}],
    }

    mock_post.side_effect = [resp_fail, resp_ok]

    contact = add_or_update_contact(phone_number="+14155552671", is_opted_in=True, db_session=api_session)
    api_session.commit()

    api = WhatsAppCloudAPI(api_token="token", phone_number_id="123", mode="production", max_retries=2)
    res = api.send_template_message(to_phone=contact.phone_number, template_name="hello_world", db_session=api_session)

    assert res["wamid"] == "wamid.HBgM_RETRY_SUCCESS"
    assert mock_post.call_count == 2


# ==============================================================================
# 6. MARKETING CONSENT GUARDRAILS
# ==============================================================================

def test_reject_message_when_recipient_lacks_consent(api_session):
    contact_no_consent = add_or_update_contact(
        phone_number="+14155550000",
        name="Unconsented",
        is_opted_in=False,  # NO CONSENT
        db_session=api_session,
    )
    api_session.commit()

    api = WhatsAppCloudAPI(mode="test")
    with pytest.raises(WhatsAppPolicyError) as exc_info:
        api.send_template_message(
            to_phone=contact_no_consent.phone_number,
            template_name="hello_world",
            contact_id=contact_no_consent.id,
            enforce_consent=True,
            db_session=api_session,
        )
    assert "active marketing consent" in str(exc_info.value).lower()


def test_reject_message_when_recipient_opted_out(api_session):
    contact_opted_out = add_or_update_contact(
        phone_number="+14155550001",
        name="Opted Out",
        is_opted_in=True,
        db_session=api_session,
    )
    record_opt_out(contact_opted_out.id, db_session=api_session)
    api_session.commit()

    api = WhatsAppCloudAPI(mode="test")
    with pytest.raises(WhatsAppPolicyError):
        api.send_template_message(
            to_phone=contact_opted_out.phone_number,
            template_name="hello_world",
            contact_id=contact_opted_out.id,
            enforce_consent=True,
            db_session=api_session,
        )


# ==============================================================================
# 7. BULK DISPATCH CONFIRMATION GUARD
# ==============================================================================

def test_bulk_dispatch_requires_explicit_confirmation():
    api = WhatsAppCloudAPI(mode="test")
    recipients = [
        {"phone_number": "+14155550001"},
        {"phone_number": "+14155550002"},
    ]

    with pytest.raises(ConfirmationRequiredError) as exc:
        api.send_bulk_template_messages(
            recipients=recipients,
            template_name="hello_world",
            confirmed=False,  # Not confirmed!
        )
    assert "requires explicit confirmation" in str(exc.value).lower()


def test_bulk_dispatch_with_confirmation_and_consent(api_session):
    c1 = add_or_update_contact(phone_number="+14155551111", is_opted_in=True, db_session=api_session)
    c2 = add_or_update_contact(phone_number="+14155552222", is_opted_in=False, db_session=api_session) # Unconsented
    api_session.commit()

    api = WhatsAppCloudAPI(mode="test")
    recipients = [
        {"phone_number": c1.phone_number, "contact_id": c1.id},
        {"phone_number": c2.phone_number, "contact_id": c2.id},
    ]

    res = api.send_bulk_template_messages(
        recipients=recipients,
        template_name="hello_world",
        confirmed=True,
        enforce_consent=True,
        db_session=api_session,
    )

    assert res["total"] == 2
    assert res["sent"] == 1
    assert res["skipped_consent"] == 1


# ==============================================================================
# 8. RATE LIMITER TOKEN TEST
# ==============================================================================

def test_rate_limiter_acquire():
    limiter = TokenBucketRateLimiter(rate_per_sec=100.0)
    # Should acquire without error
    limiter.acquire()
    limiter.acquire()
    assert limiter.tokens <= 100.0
