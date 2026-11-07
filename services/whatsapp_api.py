"""
WhatsApp Business Cloud API integration using Python requests.
Connects to Meta's official WhatsApp Graph API endpoint for template broadcast dispatches.

Features:
- Official Meta WhatsApp Cloud API endpoint (POST /{phone_number_id}/messages).
- Environment variable configuration (WHATSAPP_API_TOKEN, WHATSAPP_PHONE_NUMBER_ID, etc.).
- Approved WhatsApp template message payload formatting.
- International E.164 phone format normalization and Meta 'to' formatting.
- Automatic storage of WhatsApp message IDs (wamid) in database MessageLog & MessageStatusEvent.
- Robust timeout and exponential backoff retry for transient network/server failures (5xx, 429).
- Token-bucket rate limiting to respect Meta tier API limits.
- Sensitive token masking (never exposes raw access tokens in logs or UI).
- Default "test" mode with simulation fallback and explicit bulk confirmation guards.
- Policy enforcement: Strictly blocks dispatches to contacts without marketing consent.
"""

from datetime import datetime, timezone
import json
import logging
import random
import re
import time
from typing import Optional, List, Dict, Any, Union, Tuple
import uuid

import requests
from requests.exceptions import RequestException, Timeout, ConnectionError
from sqlalchemy import select
from sqlalchemy.orm import Session

from config.settings import (
    WHATSAPP_API_TOKEN,
    WHATSAPP_PHONE_NUMBER_ID,
    WHATSAPP_BUSINESS_ACCOUNT_ID,
    WHATSAPP_API_VERSION,
    WHATSAPP_API_BASE_URL,
    WHATSAPP_MODE,
    WHATSAPP_API_TIMEOUT,
    WHATSAPP_MAX_RETRIES,
    WHATSAPP_RATE_LIMIT_RPS,
)
from database.connection import get_db
from database.models import MessageLog, MessageStatusEvent, Contact, Campaign, CampaignRecipient
from services.contact_service import validate_and_normalize_phone

logger = logging.getLogger("campaign_platform.whatsapp_api")


# ==============================================================================
# TOKEN MASKING & LOG SECURITY
# ==============================================================================

def mask_token(token: Optional[str]) -> str:
    """
    Safely masks API tokens so they are never leaked in logs, error traces, or UI.
    Example: EAAG...****...Ab3C
    """
    if not token or token.strip() == "":
        return "[NOT_CONFIGURED]"
    token_str = token.strip()
    if len(token_str) <= 8:
        return "****"
    return f"{token_str[:4]}...{'*' * 6}...{token_str[-4:]}"


def sanitize_headers_for_logging(headers: Dict[str, str]) -> Dict[str, str]:
    """Returns headers with sensitive Authorization token redacted."""
    sanitized = dict(headers)
    if "Authorization" in sanitized:
        sanitized["Authorization"] = "Bearer [REDACTED]"
    return sanitized


# ==============================================================================
# CUSTOM DOMAIN EXCEPTIONS
# ==============================================================================

class WhatsAppAPIError(Exception):
    """Base exception for all WhatsApp Cloud API operations."""
    def __init__(self, message: str, status_code: Optional[int] = None, error_data: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.status_code = status_code
        self.error_data = error_data or {}


class WhatsAppAuthError(WhatsAppAPIError):
    """Raised when access token is invalid, missing, or expired (HTTP 401 / code 190)."""
    pass


class WhatsAppRateLimitError(WhatsAppAPIError):
    """Raised when API rate limit is exceeded (HTTP 429 / code 80007, 130429)."""
    pass


class WhatsAppPolicyError(WhatsAppAPIError):
    """Raised when WhatsApp messaging policy or recipient consent is violated."""
    pass


class WhatsAppTemplateError(WhatsAppAPIError):
    """Raised when a template is not found, unapproved, or parameters are invalid."""
    pass


class ConfirmationRequiredError(WhatsAppAPIError):
    """Raised when real bulk messaging is attempted without explicit user confirmation."""
    pass


# ==============================================================================
# RATE LIMITER
# ==============================================================================

class TokenBucketRateLimiter:
    """
    Thread-safe in-memory rate limiter ensuring dispatch rate does not exceed
    Meta API limits (requests per second).
    """
    def __init__(self, rate_per_sec: float = 20.0):
        self.rate = max(1.0, float(rate_per_sec))
        self.capacity = self.rate
        self.tokens = self.capacity
        self.last_updated = time.monotonic()

    def acquire(self) -> None:
        """Blocks until a token is available to issue the next request."""
        now = time.monotonic()
        elapsed = now - self.last_updated
        self.last_updated = now

        # Replenish tokens based on elapsed time
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)

        if self.tokens < 1.0:
            sleep_time = (1.0 - self.tokens) / self.rate
            time.sleep(sleep_time)
            self.tokens = 0.0
            self.last_updated = time.monotonic()
        else:
            self.tokens -= 1.0


# ==============================================================================
# PAYLOAD BUILDERS
# ==============================================================================

def format_meta_recipient_phone(e164_phone: str) -> str:
    """
    Converts E.164 phone (+14155552671) into Meta's required recipient format
    (plain digits without leading +: 14155552671).
    """
    return e164_phone.lstrip("+").strip()


def build_template_payload(
    to_phone: str,
    template_name: str,
    language_code: str = "en_US",
    body_parameters: Optional[List[str]] = None,
    button_parameters: Optional[List[Dict[str, str]]] = None,
    header_parameters: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Constructs the official Meta WhatsApp Cloud API JSON payload for template messages.
    """
    meta_to = format_meta_recipient_phone(to_phone)

    components = []

    # Optional Header parameters (image, document, video, or text)
    if header_parameters:
        components.append({
            "type": "header",
            "parameters": header_parameters,
        })

    # Body parameters (dynamic {{1}}, {{2}} placeholders)
    if body_parameters:
        param_list = [{"type": "text", "text": str(p)} for p in body_parameters]
        components.append({
            "type": "body",
            "parameters": param_list,
        })

    # Button parameters (e.g. dynamic URL extension codes)
    if button_parameters:
        for idx, btn in enumerate(button_parameters):
            components.append({
                "type": "button",
                "sub_type": btn.get("sub_type", "url"),
                "index": str(btn.get("index", idx)),
                "parameters": btn.get("parameters", [{"type": "text", "text": btn.get("text", "")}]),
            })

    template_payload: Dict[str, Any] = {
        "name": template_name,
        "language": {
            "code": language_code,
        },
    }
    if components:
        template_payload["components"] = components

    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": meta_to,
        "type": "template",
        "template": template_payload,
    }


# ==============================================================================
# WHATSAPP CLOUD API CLIENT
# ==============================================================================

class WhatsAppCloudAPI:
    """
    Official Meta WhatsApp Cloud API client.
    Supports single and bulk template dispatches, rate limiting, retries,
    token masking, policy checks, and database persistence.
    """

    def __init__(
        self,
        api_token: Optional[str] = None,
        phone_number_id: Optional[str] = None,
        waba_id: Optional[str] = None,
        api_version: Optional[str] = None,
        base_url: Optional[str] = None,
        mode: Optional[str] = None,
        timeout: Optional[int] = None,
        max_retries: Optional[int] = None,
        rate_limit_rps: Optional[float] = None,
    ):
        self.api_token = (api_token if api_token is not None else WHATSAPP_API_TOKEN).strip()
        self.phone_number_id = (phone_number_id if phone_number_id is not None else WHATSAPP_PHONE_NUMBER_ID).strip()
        self.waba_id = (waba_id if waba_id is not None else WHATSAPP_BUSINESS_ACCOUNT_ID).strip()
        self.api_version = api_version or WHATSAPP_API_VERSION
        self.base_url = (base_url or WHATSAPP_API_BASE_URL).rstrip("/")
        
        # Test mode is STRICTLY default unless explicitly set to 'production'
        raw_mode = (mode or WHATSAPP_MODE).lower().strip()
        self.mode = "production" if raw_mode in ("production", "live") else "test"

        self.timeout = timeout or WHATSAPP_API_TIMEOUT
        self.max_retries = max_retries or WHATSAPP_MAX_RETRIES
        self.rate_limiter = TokenBucketRateLimiter(rate_limit_rps or WHATSAPP_RATE_LIMIT_RPS)

    @property
    def messages_endpoint(self) -> str:
        """Constructs full Meta Graph API endpoint for messages."""
        return f"{self.base_url}/{self.api_version}/{self.phone_number_id}/messages"

    @property
    def is_production(self) -> bool:
        """Returns True if operating in production mode."""
        return self.mode == "production"

    def get_masked_credentials(self) -> Dict[str, Any]:
        """
        Returns safe sanitized configuration details suitable for UI display or logs.
        Never exposes the raw API token.
        """
        return {
            "mode": self.mode,
            "api_version": self.api_version,
            "phone_number_id": self.phone_number_id if self.phone_number_id else "[NOT_SET]",
            "waba_id": self.waba_id if self.waba_id else "[NOT_SET]",
            "api_token": mask_token(self.api_token),
            "is_token_configured": bool(self.api_token and self.api_token != "your_whatsapp_token_here"),
            "is_phone_configured": bool(self.phone_number_id and self.phone_number_id != "your_phone_number_id_here"),
            "endpoint": f"{self.base_url}/{self.api_version}/{self.phone_number_id or '{PHONE_ID}'}/messages",
        }

    def check_health(self) -> Dict[str, Any]:
        """
        Verifies connectivity and permissions against Meta's Graph API.
        In test mode without credentials, returns a healthy simulation status.
        """
        if self.mode == "test" and (not self.api_token or self.api_token == "your_whatsapp_token_here"):
            return {
                "status": "healthy",
                "mode": "test (simulation)",
                "connected": True,
                "message": "Test mode active with simulated Meta Cloud API responses.",
                "credentials": self.get_masked_credentials(),
            }

        if not self.api_token or not self.phone_number_id:
            return {
                "status": "unconfigured",
                "mode": self.mode,
                "connected": False,
                "error": "WHATSAPP_API_TOKEN or WHATSAPP_PHONE_NUMBER_ID is missing.",
                "credentials": self.get_masked_credentials(),
            }

        url = f"{self.base_url}/{self.api_version}/{self.phone_number_id}"
        headers = {
            "Authorization": f"Bearer {self.api_token}",
        }

        try:
            resp = requests.get(url, headers=headers, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "status": "healthy",
                    "mode": self.mode,
                    "connected": True,
                    "phone_details": {
                        "id": data.get("id"),
                        "verified_name": data.get("verified_name", "N/A"),
                        "display_phone_number": data.get("display_phone_number", "N/A"),
                        "quality_rating": data.get("quality_rating", "GREEN"),
                    },
                    "credentials": self.get_masked_credentials(),
                }
            else:
                err_data = resp.json().get("error", {})
                return {
                    "status": "unhealthy",
                    "mode": self.mode,
                    "connected": False,
                    "http_status": resp.status_code,
                    "error_code": err_data.get("code"),
                    "error_message": err_data.get("message", resp.text),
                    "credentials": self.get_masked_credentials(),
                }
        except Exception as e:
            return {
                "status": "unreachable",
                "mode": self.mode,
                "connected": False,
                "error": str(e),
                "credentials": self.get_masked_credentials(),
            }

    def _execute_http_post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Sends HTTP POST request to Meta Cloud API with rate limiting and exponential backoff retry.
        Handles transient 5xx / 429 errors while fast-failing client errors (400, 401, 403).
        """
        self.rate_limiter.acquire()

        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
        }

        retries = 0
        backoff = 1.0

        while True:
            try:
                logger.info(
                    f"Dispatching to WhatsApp API endpoint: {self.messages_endpoint} "
                    f"[Recipient: {payload.get('to')}, Template: {payload.get('template', {}).get('name')}]"
                )

                response = requests.post(
                    self.messages_endpoint,
                    headers=headers,
                    json=payload,
                    timeout=self.timeout,
                )

                # Successful API response
                if response.status_code in (200, 201):
                    return response.json()

                # Handle API Errors
                try:
                    error_json = response.json().get("error", {})
                except Exception:
                    error_json = {"message": response.text}

                error_code = error_json.get("code")
                error_msg = error_json.get("message", f"HTTP {response.status_code} error from Meta API")

                # Authentication error: invalid or expired token
                if response.status_code == 401 or error_code in (190, 102):
                    raise WhatsAppAuthError(f"WhatsApp authentication failed: {error_msg}", status_code=401, error_data=error_json)

                # Rate limit error: HTTP 429 or Meta rate limit codes
                if response.status_code == 429 or error_code in (80007, 130429):
                    if retries < self.max_retries:
                        retries += 1
                        retry_after = int(response.headers.get("Retry-After", int(backoff)))
                        logger.warning(f"Rate limited by Meta API. Retrying in {retry_after}s (attempt {retries}/{self.max_retries})...")
                        time.sleep(retry_after)
                        backoff *= 2.0
                        continue
                    raise WhatsAppRateLimitError(f"Meta rate limit exceeded: {error_msg}", status_code=429, error_data=error_json)

                # Template error
                if error_code in (132000, 132001, 132005, 132015):
                    raise WhatsAppTemplateError(f"WhatsApp template error: {error_msg}", status_code=response.status_code, error_data=error_json)

                # Policy or user error
                if error_code in (131030, 131047, 131051, 131052):
                    raise WhatsAppPolicyError(f"WhatsApp policy violation: {error_msg}", status_code=response.status_code, error_data=error_json)

                # Transient server errors (5xx)
                if response.status_code >= 500 and retries < self.max_retries:
                    retries += 1
                    sleep_time = backoff + random.uniform(0.1, 0.5)
                    logger.warning(f"Transient error HTTP {response.status_code} from Meta. Retrying in {sleep_time:.2f}s...")
                    time.sleep(sleep_time)
                    backoff *= 2.0
                    continue

                # Any other non-retryable 4xx error
                raise WhatsAppAPIError(
                    f"WhatsApp API request failed [HTTP {response.status_code} / Code {error_code}]: {error_msg}",
                    status_code=response.status_code,
                    error_data=error_json,
                )

            except (Timeout, ConnectionError) as exc:
                if retries < self.max_retries:
                    retries += 1
                    sleep_time = backoff + random.uniform(0.1, 0.5)
                    logger.warning(f"Network error: {exc}. Retrying in {sleep_time:.2f}s (attempt {retries}/{self.max_retries})...")
                    time.sleep(sleep_time)
                    backoff *= 2.0
                    continue
                raise WhatsAppAPIError(f"Network connection failed after {self.max_retries} retries: {exc}") from exc

    def send_template_message(
        self,
        to_phone: str,
        template_name: str,
        language_code: str = "en_US",
        body_parameters: Optional[List[str]] = None,
        button_parameters: Optional[List[Dict[str, str]]] = None,
        header_parameters: Optional[List[Dict[str, Any]]] = None,
        contact_id: Optional[int] = None,
        campaign_id: Optional[int] = None,
        campaign_recipient_id: Optional[int] = None,
        enforce_consent: bool = True,
        db_session: Optional[Session] = None,
    ) -> Dict[str, Any]:
        """
        Dispatches an approved WhatsApp template message to a single recipient.
        
        Validations:
        1. Normalizes phone to international E.164.
        2. Policy check: Ensures recipient has verified opt-in marketing consent.
        3. Test/Production Mode execution.
        4. Saves WhatsApp message ID (wamid) in SQLite database MessageLog table.
        """
        # 1. Normalize phone number
        is_valid, normalized_phone, reason = validate_and_normalize_phone(to_phone)
        if not is_valid:
            raise ValueError(f"Invalid international mobile number format '{to_phone}': {reason}")

        # 2. Enforce Marketing Consent
        if enforce_consent:
            def _check_consent(session: Session) -> Optional[Contact]:
                if contact_id:
                    return session.get(Contact, contact_id)
                return session.execute(
                    select(Contact).where(Contact.phone_number == normalized_phone)
                ).scalar_one_or_none()

            contact_obj = _check_consent(db_session) if db_session else None
            if not contact_obj:
                with get_db() as s:
                    contact_obj = _check_consent(s)

            if contact_obj:
                if not contact_obj.is_opted_in or contact_obj.status == "unsubscribed":
                    raise WhatsAppPolicyError(
                        f"Cannot send marketing template: Contact {normalized_phone} does not have active marketing consent."
                    )
                if not contact_id:
                    contact_id = contact_obj.id

        # 3. Build official Meta payload
        payload = build_template_payload(
            to_phone=normalized_phone,
            template_name=template_name,
            language_code=language_code,
            body_parameters=body_parameters,
            button_parameters=button_parameters,
            header_parameters=header_parameters,
        )

        # 4. Dispatch (Live API or Test Simulation)
        now_utc = datetime.now(timezone.utc)
        response_json: Dict[str, Any] = {}

        if self.mode == "test" and (not self.api_token or self.api_token == "your_whatsapp_token_here"):
            # Safe simulated test dispatch (Meta developer sandbox simulation)
            simulated_wamid = f"wamid.HBgM_SIMULATED_{int(now_utc.timestamp())}_{uuid.uuid4().hex[:10]}"
            response_json = {
                "messaging_product": "whatsapp",
                "contacts": [{"input": format_meta_recipient_phone(normalized_phone), "wa_id": format_meta_recipient_phone(normalized_phone)}],
                "messages": [{"id": simulated_wamid}],
                "_simulation": True,
                "_mode": "test",
            }
            wamid = simulated_wamid
            logger.info(f"[TEST MODE] Simulated dispatch to {normalized_phone} with mock WAMID: {wamid}")
        else:
            # Real Meta Cloud API Dispatch
            response_json = self._execute_http_post(payload)
            messages = response_json.get("messages", [])
            wamid = messages[0].get("id") if messages else None
            if not wamid:
                raise WhatsAppAPIError("Meta API response missing message ID", error_data=response_json)

        # 5. Persist to Database MessageLog
        def _persist_message(session: Session) -> MessageLog:
            # If contact does not exist in DB yet, auto-register
            cid = contact_id
            if not cid:
                c = session.execute(
                    select(Contact).where(Contact.phone_number == normalized_phone)
                ).scalar_one_or_none()
                if c:
                    cid = c.id
                else:
                    new_contact = Contact(
                        phone_number=normalized_phone,
                        is_opted_in=True if not enforce_consent else False,
                        status="active",
                    )
                    session.add(new_contact)
                    session.flush()
                    cid = new_contact.id

            log_entry = MessageLog(
                whatsapp_message_id=wamid,
                campaign_id=campaign_id,
                contact_id=cid,
                campaign_recipient_id=campaign_recipient_id,
                phone_number=normalized_phone,
                message_type="template",
                message_content=json.dumps({"template": template_name, "parameters": body_parameters}),
                status="sent",
                sent_at=now_utc,
            )
            session.add(log_entry)
            session.flush()

            # Record initial status event
            status_event = MessageStatusEvent(
                message_log_id=log_entry.id,
                status="sent",
                event_timestamp=now_utc,
                raw_payload=json.dumps(response_json),
            )
            session.add(status_event)

            # Update campaign counter if attached
            if campaign_id:
                camp = session.get(Campaign, campaign_id)
                if camp:
                    camp.sent_count = (camp.sent_count or 0) + 1

            # Update recipient status if attached
            if campaign_recipient_id:
                recip = session.get(CampaignRecipient, campaign_recipient_id)
                if recip:
                    recip.status = "sent"
                    recip.sent_at = now_utc

            session.flush()
            return log_entry

        if db_session:
            db_log = _persist_message(db_session)
        else:
            with get_db() as s:
                db_log = _persist_message(s)

        return {
            "status": "sent",
            "wamid": wamid,
            "phone_number": normalized_phone,
            "message_log_id": db_log.id,
            "mode": self.mode,
            "raw_response": response_json,
        }

    def send_bulk_template_messages(
        self,
        recipients: List[Dict[str, Any]],
        template_name: str,
        language_code: str = "en_US",
        campaign_id: Optional[int] = None,
        confirmed: bool = False,
        enforce_consent: bool = True,
        db_session: Optional[Session] = None,
    ) -> Dict[str, Any]:
        """
        Broadcasts template messages to a list of recipients.
        
        Safety Guards:
        1. In test mode, limits maximum test dispatches to 5 recipients to prevent accidents.
        2. In production mode, requires `confirmed=True` to prevent unintentional mass dispatches.
        3. Strictly excludes non-consented contacts.
        4. Applies rate limiting between dispatches.
        """
        # Safety Check 1: Confirmation requirement for bulk
        if not confirmed and len(recipients) > 1:
            raise ConfirmationRequiredError(
                f"Safety Guard: Bulk campaign of {len(recipients)} recipients requires explicit confirmation (confirmed=True)."
            )

        # Safety Check 2: Test mode recipient threshold
        if self.mode == "test" and len(recipients) > 10:
            raise WhatsAppPolicyError(
                "Test mode safety limit: Maximum 10 test recipients allowed in test mode. "
                "Switch to production mode with verified WABA credentials for mass broadcasts."
            )

        total = len(recipients)
        sent_count = 0
        failed_count = 0
        skipped_consent_count = 0
        results = []

        for r in recipients:
            phone = r.get("phone_number") or r.get("phone")
            cid = r.get("contact_id") or r.get("id")
            recipient_id = r.get("campaign_recipient_id")
            params = r.get("body_parameters", [])

            try:
                res = self.send_template_message(
                    to_phone=phone,
                    template_name=template_name,
                    language_code=language_code,
                    body_parameters=params,
                    contact_id=cid,
                    campaign_id=campaign_id,
                    campaign_recipient_id=recipient_id,
                    enforce_consent=enforce_consent,
                    db_session=db_session,
                )
                sent_count += 1
                results.append({"phone": phone, "status": "sent", "wamid": res["wamid"]})
            except WhatsAppPolicyError as pe:
                skipped_consent_count += 1
                results.append({"phone": phone, "status": "skipped", "reason": str(pe)})
            except Exception as exc:
                failed_count += 1
                results.append({"phone": phone, "status": "failed", "error": str(exc)})

        return {
            "total": total,
            "sent": sent_count,
            "failed": failed_count,
            "skipped_consent": skipped_consent_count,
            "mode": self.mode,
            "results": results,
        }


# Global default client instance
whatsapp_client = WhatsAppCloudAPI()
