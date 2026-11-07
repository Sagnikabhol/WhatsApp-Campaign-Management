"""
Contact service: Business logic for parsing, validating, deduplicating, and managing contacts.
Includes international phone number format validation, E.164 normalization,
WhatsApp marketing consent lifecycle management (opt-in, opt-out, audit trails),
and CSV list ingestion with duplicate prevention.
"""

from datetime import datetime, timezone
import json
import logging
import re
from pathlib import Path
from typing import Optional, List, Dict, Any, Union, Tuple

import pandas as pd
from sqlalchemy import select, func, or_, desc
from sqlalchemy.orm import Session

from database.connection import get_db, safe_commit, DuplicateRecordError, RecordNotFoundError
from database.models import Contact, ConsentRecord

logger = logging.getLogger("campaign_platform.contacts")


# ==============================================================================
# PHONE NUMBER VALIDATION & NORMALIZATION (E.164 STANDARD)
# ==============================================================================

# Regex for standard E.164 phone numbers (+ followed by 7 to 15 digits; first digit cannot be 0)
E164_REGEX = re.compile(r"^\+[1-9]\d{6,14}$")


def validate_and_normalize_phone(
    raw_phone: Any,
    default_country_code: Optional[str] = None,
) -> Tuple[bool, str, str]:
    """
    Validates and normalizes international mobile numbers according to ITU-T E.164.

    Rules:
    - Strips whitespace, hyphens, brackets, dots, and slashes.
    - Converts international trunk prefix '00' to '+'.
    - If '+' is missing:
      - If default_country_code is provided and number is 10 digits, prepends country code.
      - If digits length is 10-15 and starts with 1-9, prepends '+'.
    - Validates that total digits are between 7 and 15 digits.
    - Validates that country code does not start with 0.
    - Detects non-numeric characters (alphabetic, illegal symbols).

    Returns:
        (is_valid: bool, normalized_phone: str, message: str)
    """
    if raw_phone is None or pd.isna(raw_phone):
        return False, "", "Phone number is empty or missing"

    raw_str = str(raw_phone).strip()
    if not raw_str:
        return False, "", "Phone number is empty"

    # Remove standard formatting delimiters: spaces, hyphens, dots, parentheses, slashes
    cleaned = re.sub(r"[\s\-\.\(\)\/]", "", raw_str)

    # Check for illegal characters (letters, punctuation other than leading +)
    test_chars = cleaned[1:] if cleaned.startswith("+") else cleaned
    if not test_chars.isdigit():
        return False, raw_str, "Contains non-numeric characters or invalid symbols"

    # Handle international call prefix '00' (e.g., 0012345678901 -> +12345678901)
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]

    # Normalize leading +
    if not cleaned.startswith("+"):
        if cleaned.startswith("0") and default_country_code:
            code = default_country_code.lstrip("+")
            cleaned = f"+{code}{cleaned[1:]}"
        elif default_country_code and len(cleaned) == 10:
            code = default_country_code.lstrip("+")
            cleaned = f"+{code}{cleaned}"
        elif 10 <= len(cleaned) <= 15 and not cleaned.startswith("0"):
            cleaned = f"+{cleaned}"
        else:
            return False, raw_str, "Missing international country code (e.g., +1, +91, +44)"

    digits_only = cleaned[1:]
    if not digits_only.isdigit():
        return False, raw_str, "Malformed phone number after prefix removal"

    digit_count = len(digits_only)
    if digit_count < 7:
        return False, raw_str, f"Too short ({digit_count} digits, minimum is 7)"
    if digit_count > 15:
        return False, raw_str, f"Too long ({digit_count} digits, maximum is 15 for E.164)"

    if digits_only.startswith("0"):
        return False, raw_str, "Invalid international country code (cannot start with 0)"

    if not E164_REGEX.match(cleaned):
        return False, raw_str, "Does not conform to E.164 international standard"

    return True, cleaned, "Valid international format"


def normalize_phone_number(phone: str) -> str:
    """Convenience wrapper returning the normalized string or empty string on failure."""
    is_valid, normalized, _ = validate_and_normalize_phone(phone)
    return normalized if is_valid else ""


# ==============================================================================
# COLUMN DETECTION & CSV BATCH PROCESSING
# ==============================================================================

def detect_contact_columns(columns: List[str]) -> Dict[str, Optional[str]]:
    """
    Intelligently maps CSV column headers to required contact fields.
    Detects name, phone, email, tags, custom variables, and explicit consent columns.
    """
    normalized_cols = {str(c).lower().strip().replace(" ", "_"): c for c in columns}

    name_aliases = [
        "name", "full_name", "fullname", "contact_name",
        "first_name", "customer_name", "recipient_name", "client_name"
    ]
    phone_aliases = [
        "phone_number", "phone", "mobile_number", "mobile",
        "contact_number", "phone_no", "mobile_no", "cell", "tel", "whatsapp"
    ]
    email_aliases = ["email", "email_address", "mail"]
    tag_aliases = ["tags", "tag", "segment", "groups", "group"]
    custom_aliases = ["custom_variable", "custom_attributes", "custom_var", "variable", "notes"]
    consent_aliases = [
        "consent", "opt_in", "optin", "opted_in", "marketing_consent",
        "whatsapp_consent", "permission", "consent_status", "opt_in_status"
    ]

    name_col = next((normalized_cols[a] for a in name_aliases if a in normalized_cols), None)
    phone_col = next((normalized_cols[a] for a in phone_aliases if a in normalized_cols), None)
    email_col = next((normalized_cols[a] for a in email_aliases if a in normalized_cols), None)
    tag_col = next((normalized_cols[a] for a in tag_aliases if a in normalized_cols), None)
    custom_col = next((normalized_cols[a] for a in custom_aliases if a in normalized_cols), None)
    consent_col = next((normalized_cols[a] for a in consent_aliases if a in normalized_cols), None)

    return {
        "name": name_col,
        "phone": phone_col,
        "email": email_col,
        "tags": tag_col,
        "custom": custom_col,
        "consent": consent_col,
    }


def parse_consent_field(val: Any) -> Optional[bool]:
    """
    Parses a field value into a boolean consent flag.
    Returns True for explicit opt-in values, False for opt-out/no-consent, or None if unparseable/empty.
    """
    if val is None or pd.isna(val):
        return None
    s = str(val).strip().lower()
    if s in ("1", "true", "yes", "y", "opt_in", "opt-in", "opted_in", "granted", "approved", "active", "eligible"):
        return True
    if s in ("0", "false", "no", "n", "opt_out", "opt-out", "revoked", "denied", "unsubscribed", "ineligible"):
        return False
    return None


def process_and_validate_contacts(
    df: pd.DataFrame,
    name_col: Optional[str] = None,
    phone_col: Optional[str] = None,
    email_col: Optional[str] = None,
    tag_col: Optional[str] = None,
    custom_col: Optional[str] = None,
    consent_col: Optional[str] = None,
    default_country_code: Optional[str] = None,
    default_opt_in: bool = False,  # CRITICAL: Never assume marketing consent by default!
    check_db: bool = True,
    db_session: Optional[Session] = None,
) -> Tuple[pd.DataFrame, Dict[str, int]]:
    """
    Processes an input DataFrame of contacts:
    - Maps name, mobile number, and consent.
    - Validates international phone format.
    - Normalizes to E.164.
    - Detects in-file duplicates and database duplicates.
    - Evaluates explicit WhatsApp marketing consent per contact without assuming consent.
    - Categorizes records as VALID, INVALID_FORMAT, DUPLICATE_IN_FILE, or ALREADY_IN_DATABASE.
    """
    if df.empty:
        empty_res = pd.DataFrame(columns=[
            "Name", "Original Number", "Normalized Number", "Email", "Tags",
            "Custom Variable", "Consent Status", "Campaign Eligibility",
            "Validation Status", "Validation Reason"
        ])
        return empty_res, {
            "total": 0, "valid": 0, "invalid": 0, "duplicate_file": 0,
            "duplicate_db": 0, "duplicate_total": 0, "eligible": 0, "ineligible": 0
        }

    # Auto-detect columns if not explicitly passed
    detected = detect_contact_columns(list(df.columns))
    name_key = name_col or detected["name"] or df.columns[0]
    phone_key = phone_col or detected["phone"]
    if not phone_key:
        phone_key = df.columns[1] if len(df.columns) > 1 else df.columns[0]

    email_key = email_col or detected["email"]
    tag_key = tag_col or detected["tags"]
    custom_key = custom_col or detected["custom"]
    consent_key = consent_col or detected["consent"]

    # Retrieve existing phone numbers from database
    existing_db_phones = set()
    if check_db:
        def _fetch_db_phones(session: Session) -> set:
            return set(session.execute(select(Contact.phone_number)).scalars().all())

        if db_session is not None:
            existing_db_phones = _fetch_db_phones(db_session)
        else:
            with get_db() as session:
                existing_db_phones = _fetch_db_phones(session)

    processed_rows = []
    seen_in_file: Dict[str, int] = {}

    valid_cnt = 0
    invalid_cnt = 0
    dup_file_cnt = 0
    dup_db_cnt = 0
    eligible_cnt = 0
    ineligible_cnt = 0

    for idx, row in df.iterrows():
        raw_name = str(row.get(name_key, "")).strip() if pd.notna(row.get(name_key)) else ""
        raw_phone = row.get(phone_key, "")
        raw_email = str(row.get(email_key, "")).strip() if email_key and pd.notna(row.get(email_key)) else ""
        raw_tags = str(row.get(tag_key, "")).strip() if tag_key and pd.notna(row.get(tag_key)) else ""
        raw_custom = str(row.get(custom_key, "")).strip() if custom_key and pd.notna(row.get(custom_key)) else ""

        # Determine consent: explicit column take precedence, else default_opt_in (defaults to False)
        row_consent: bool = False
        consent_source: str = "none"

        if consent_key and pd.notna(row.get(consent_key)):
            parsed = parse_consent_field(row.get(consent_key))
            if parsed is not None:
                row_consent = parsed
                consent_source = "csv_column"
            else:
                row_consent = default_opt_in
                consent_source = "user_override" if default_opt_in else "none"
        else:
            row_consent = default_opt_in
            consent_source = "user_override" if default_opt_in else "none"

        is_valid, normalized_phone, reason = validate_and_normalize_phone(
            raw_phone, default_country_code=default_country_code
        )

        status = "VALID"

        if not is_valid:
            status = "INVALID_FORMAT"
            invalid_cnt += 1
        elif normalized_phone in seen_in_file:
            status = "DUPLICATE_IN_FILE"
            reason = f"Duplicate number in CSV (already seen in row {seen_in_file[normalized_phone]})"
            dup_file_cnt += 1
        elif check_db and normalized_phone in existing_db_phones:
            status = "ALREADY_IN_DATABASE"
            reason = "Number already registered in platform database"
            dup_db_cnt += 1
        else:
            seen_in_file[normalized_phone] = idx + 1
            valid_cnt += 1

        is_eligible = (status == "VALID" and row_consent)
        if is_eligible:
            eligible_cnt += 1
        else:
            ineligible_cnt += 1

        processed_rows.append({
            "Name": raw_name or "Unknown",
            "Original Number": str(raw_phone) if pd.notna(raw_phone) else "",
            "Normalized Number": normalized_phone if is_valid else "",
            "Email": raw_email,
            "Tags": raw_tags,
            "Custom Variable": raw_custom,
            "Consent Status": "OPT_IN" if row_consent else "NO_CONSENT",
            "Consent Source": consent_source,
            "Campaign Eligibility": "Eligible" if is_eligible else "Ineligible (No Consent)" if status == "VALID" else "Ineligible (Invalid/Dup)",
            "Validation Status": status,
            "Validation Reason": reason,
        })

    result_df = pd.DataFrame(processed_rows)
    counts = {
        "total": len(df),
        "valid": valid_cnt,
        "invalid": invalid_cnt,
        "duplicate_file": dup_file_cnt,
        "duplicate_db": dup_db_cnt,
        "duplicate_total": dup_file_cnt + dup_db_cnt,
        "eligible": eligible_cnt,
        "ineligible": ineligible_cnt,
    }

    return result_df, counts


# ==============================================================================
# DATABASE STORAGE & CONSENT AUDIT LOGGING
# ==============================================================================

def save_validated_contacts(
    validated_df: pd.DataFrame,
    default_opt_in: bool = False,  # Never assume consent!
    consent_source: str = "csv_import",
    consent_proof: Optional[str] = None,
    db_session: Optional[Session] = None,
) -> Dict[str, int]:
    """
    Persists VALID contacts into SQLite database.
    Prevents duplicate insertions.
    Records auditable consent records reflecting true consent status (OPT_IN or NO_CONSENT).
    """
    valid_rows = validated_df[validated_df["Validation Status"] == "VALID"]
    if valid_rows.empty:
        return {"saved": 0, "skipped": 0, "errors": 0}

    saved_count = 0
    skipped_count = 0
    error_count = 0

    def _execute(session: Session) -> Dict[str, int]:
        nonlocal saved_count, skipped_count, error_count
        now_utc = datetime.now(timezone.utc)

        for _, row in valid_rows.iterrows():
            phone = row["Normalized Number"]
            name = row["Name"] if row["Name"] != "Unknown" else None
            email = row["Email"] if row["Email"] else None
            tags = row["Tags"] if row["Tags"] else None
            custom_var = row["Custom Variable"] if row["Custom Variable"] else None
            custom_attr_json = json.dumps({"custom_variable": custom_var}) if custom_var else None

            # Determine opt-in flag from row or default
            if "Consent Status" in row:
                is_opted_in = (row["Consent Status"] == "OPT_IN")
            else:
                is_opted_in = bool(default_opt_in)

            # Prevent duplicate insertion
            existing = session.execute(
                select(Contact).where(Contact.phone_number == phone)
            ).scalar_one_or_none()

            if existing:
                skipped_count += 1
                continue

            try:
                contact = Contact(
                    phone_number=phone,
                    name=name,
                    email=email,
                    tags=tags,
                    custom_attributes=custom_attr_json,
                    is_opted_in=is_opted_in,
                    status="active",
                )
                session.add(contact)
                session.flush()

                # Always create an auditable consent record
                consent_status_str = "OPT_IN" if is_opted_in else "NO_CONSENT"
                consent_source_str = row.get("Consent Source", consent_source) if "Consent Source" in row else consent_source
                proof_text = consent_proof or (
                    f"Verified opt-in uploaded on {now_utc.strftime('%Y-%m-%d %H:%M:%S UTC')}"
                    if is_opted_in
                    else "Uploaded without verified marketing consent"
                )

                consent = ConsentRecord(
                    contact_id=contact.id,
                    status=consent_status_str,
                    source=consent_source_str,
                    proof=proof_text,
                    consent_timestamp=now_utc,
                )
                session.add(consent)
                saved_count += 1
            except Exception as e:
                logger.error(f"Error saving contact {phone}: {e}")
                error_count += 1

        session.flush()
        return {"saved": saved_count, "skipped": skipped_count, "errors": error_count}

    if db_session is not None:
        return _execute(db_session)
    else:
        with get_db() as session:
            return _execute(session)


# ==============================================================================
# CONSENT LIFECYCLE MANAGEMENT (OPT-IN, OPT-OUT, AUDIT TRAIL)
# ==============================================================================

def record_consent(
    contact_id: int,
    status: str = "OPT_IN",
    source: str = "manual",
    proof: Optional[str] = None,
    notes: Optional[str] = None,
    consent_timestamp: Optional[datetime] = None,
    db_session: Optional[Session] = None,
) -> ConsentRecord:
    """
    Records an opt-in or opt-out event for a contact and updates marketing eligibility.
    """
    def _execute(session: Session) -> ConsentRecord:
        contact = session.get(Contact, contact_id)
        if not contact:
            raise RecordNotFoundError(f"Contact ID {contact_id} not found.")

        status_upper = status.upper()
        now_ts = consent_timestamp or datetime.now(timezone.utc)

        consent_record = ConsentRecord(
            contact_id=contact.id,
            status=status_upper,
            source=source,
            proof=proof,
            notes=notes,
            consent_timestamp=now_ts,
        )
        session.add(consent_record)

        if status_upper == "OPT_IN":
            contact.is_opted_in = True
            contact.status = "active"
        elif status_upper in ("OPT_OUT", "REVOKED"):
            contact.is_opted_in = False
            contact.status = "unsubscribed"
        elif status_upper == "NO_CONSENT":
            contact.is_opted_in = False

        session.flush()
        return consent_record

    if db_session is not None:
        return _execute(db_session)
    else:
        with get_db() as session:
            return _execute(session)


def record_opt_out(
    contact_id: int,
    source: str = "opt_out_request",
    proof: Optional[str] = None,
    notes: Optional[str] = None,
    db_session: Optional[Session] = None,
) -> ConsentRecord:
    """
    Convenience method to record an explicit opt-out request (e.g., user sends STOP).
    Immediately marks contact as ineligible for marketing campaigns.
    """
    return record_consent(
        contact_id=contact_id,
        status="OPT_OUT",
        source=source,
        proof=proof or "User requested unsubscribe / STOP",
        notes=notes,
        db_session=db_session,
    )


def record_opt_out_by_phone(
    phone_number: str,
    source: str = "whatsapp_stop",
    proof: Optional[str] = None,
    notes: Optional[str] = None,
    db_session: Optional[Session] = None,
) -> Optional[ConsentRecord]:
    """Records opt-out for a contact identified by phone number."""
    contact = get_contact_by_phone(phone_number, db_session=db_session)
    if not contact:
        logger.warning(f"Cannot record opt-out: Phone {phone_number} not found.")
        return None
    return record_opt_out(contact.id, source=source, proof=proof, notes=notes, db_session=db_session)


def record_opt_in(
    contact_id: int,
    source: str = "user_consent",
    proof: Optional[str] = None,
    notes: Optional[str] = None,
    db_session: Optional[Session] = None,
) -> ConsentRecord:
    """
    Convenience method to record an explicit opt-in event.
    Marks contact as eligible for marketing campaigns.
    """
    return record_consent(
        contact_id=contact_id,
        status="OPT_IN",
        source=source,
        proof=proof or "Explicit opt-in recorded",
        notes=notes,
        db_session=db_session,
    )


def get_consent_history(contact_id: int, db_session: Optional[Session] = None) -> List[Dict[str, Any]]:
    """
    Returns full chronological audit trail of consent events for a contact.
    Ordered latest first.
    """
    def _execute(session: Session) -> List[Dict[str, Any]]:
        records = session.execute(
            select(ConsentRecord)
            .where(ConsentRecord.contact_id == contact_id)
            .order_by(desc(ConsentRecord.consent_timestamp), desc(ConsentRecord.id))
        ).scalars().all()

        return [
            {
                "id": r.id,
                "status": r.status,
                "source": r.source or "N/A",
                "timestamp": r.consent_timestamp.strftime("%Y-%m-%d %H:%M:%S UTC") if r.consent_timestamp else "N/A",
                "proof": r.proof or "None",
                "notes": r.notes or "None",
            }
            for r in records
        ]

    if db_session is not None:
        return _execute(db_session)
    else:
        with get_db() as session:
            return _execute(session)


# ==============================================================================
# AUDIENCE QUERYING & REPORTING
# ==============================================================================

def count_contacts(db_session: Optional[Session] = None) -> Dict[str, Any]:
    """
    Returns platform-wide audience counts:
    - total: All saved contacts
    - eligible: Active contacts with valid opt-in consent
    - ineligible: Contacts without consent or explicitly opted-out
    - opted_out: Explicitly unsubscribed contacts
    - pending_consent: Contacts without consent that have not explicitly unsubscribed
    - opt_in_rate: Percentage of total contacts eligible for campaigns
    """
    def _execute(session: Session) -> Dict[str, Any]:
        total = session.scalar(select(func.count(Contact.id))) or 0
        eligible = session.scalar(
            select(func.count(Contact.id)).where(
                Contact.is_opted_in.is_(True),
                Contact.status == "active",
            )
        ) or 0
        opted_out = session.scalar(
            select(func.count(Contact.id)).where(Contact.status == "unsubscribed")
        ) or 0
        ineligible = total - eligible
        pending_consent = session.scalar(
            select(func.count(Contact.id)).where(
                Contact.is_opted_in.is_(False),
                Contact.status == "active",
            )
        ) or 0
        opt_in_rate = round((eligible / total * 100), 1) if total > 0 else 0.0

        return {
            "total": total,
            "eligible": eligible,
            "ineligible": ineligible,
            "opted_out": opted_out,
            "pending_consent": pending_consent,
            "opt_in_rate": opt_in_rate,
        }

    if db_session is not None:
        return _execute(db_session)
    else:
        with get_db() as session:
            return _execute(session)


def get_contacts_as_df(
    search_term: Optional[str] = None,
    consent_filter: Optional[str] = None,
    limit: int = 1000,
    db_session: Optional[Session] = None,
) -> pd.DataFrame:
    """
    Retrieves stored contacts from SQLite and returns a formatted DataFrame
    including consent status, campaign eligibility, and latest consent event.
    """
    def _execute(session: Session) -> pd.DataFrame:
        stmt = select(Contact)

        if search_term:
            term = f"%{search_term.strip()}%"
            stmt = stmt.where(
                or_(
                    Contact.name.ilike(term),
                    Contact.phone_number.ilike(term),
                    Contact.email.ilike(term),
                    Contact.tags.ilike(term),
                )
            )

        if consent_filter in ("Eligible (Opted In)", "Opted In"):
            stmt = stmt.where(Contact.is_opted_in.is_(True), Contact.status == "active")
        elif consent_filter in ("Ineligible", "Ineligible (No Consent / Opted Out)"):
            stmt = stmt.where(or_(Contact.is_opted_in.is_(False), Contact.status != "active"))
        elif consent_filter in ("Opted Out", "Explicitly Opted Out"):
            stmt = stmt.where(Contact.status == "unsubscribed")
        elif consent_filter in ("Pending Consent", "No Consent"):
            stmt = stmt.where(Contact.is_opted_in.is_(False), Contact.status == "active")

        stmt = stmt.order_by(Contact.id.desc()).limit(limit)
        contacts = session.execute(stmt).scalars().all()

        rows = []
        for c in contacts:
            latest_consent = c.consent_records[0] if c.consent_records else None

            if c.is_opted_in and c.status == "active":
                consent_badge = "✅ Opted In"
                eligible_label = "Yes"
            elif c.status == "unsubscribed" or (latest_consent and latest_consent.status in ("OPT_OUT", "REVOKED")):
                consent_badge = "⛔ Opted Out"
                eligible_label = "No"
            else:
                consent_badge = "⏳ No Consent"
                eligible_label = "No"

            rows.append({
                "ID": c.id,
                "Name": c.name or "N/A",
                "Mobile Number": c.phone_number,
                "Email": c.email or "N/A",
                "Marketing Consent": consent_badge,
                "Eligible for Campaigns": eligible_label,
                "Consent Source": latest_consent.source if latest_consent else "N/A",
                "Consent Date": latest_consent.consent_timestamp.strftime("%Y-%m-%d %H:%M") if latest_consent and latest_consent.consent_timestamp else "N/A",
                "Status": c.status.capitalize(),
                "Tags": c.tags or "",
                "Added At": c.created_at.strftime("%Y-%m-%d %H:%M") if c.created_at else "N/A",
            })

        return pd.DataFrame(rows)

    if db_session is not None:
        return _execute(db_session)
    else:
        with get_db() as session:
            return _execute(session)


def delete_contact(contact_id: int, db_session: Optional[Session] = None) -> bool:
    """Deletes a contact by ID (cascade handles consent records)."""
    def _execute(session: Session) -> bool:
        contact = session.get(Contact, contact_id)
        if not contact:
            return False
        session.delete(contact)
        session.flush()
        return True

    if db_session is not None:
        return _execute(db_session)
    else:
        with get_db() as session:
            return _execute(session)


def add_or_update_contact(
    phone_number: str,
    name: Optional[str] = None,
    email: Optional[str] = None,
    tags: Optional[str] = None,
    custom_attributes: Optional[Union[Dict[str, Any], str]] = None,
    is_opted_in: bool = False,
    status: str = "active",
    db_session: Optional[Session] = None,
) -> Contact:
    """Safely creates or updates a subscriber contact in the database."""
    is_valid, normalized_phone, _ = validate_and_normalize_phone(phone_number)
    if not is_valid:
        raise ValueError(f"Invalid phone number format: {phone_number}")

    custom_attr_str = (
        json.dumps(custom_attributes)
        if isinstance(custom_attributes, dict)
        else custom_attributes
    )

    def _execute(session: Session) -> Contact:
        existing = session.execute(
            select(Contact).where(Contact.phone_number == normalized_phone)
        ).scalar_one_or_none()

        if existing:
            if name is not None:
                existing.name = name
            if email is not None:
                existing.email = email
            if tags is not None:
                existing.tags = tags
            if custom_attr_str is not None:
                existing.custom_attributes = custom_attr_str
            existing.is_opted_in = is_opted_in
            existing.status = status
            session.flush()
            return existing
        else:
            contact = Contact(
                phone_number=normalized_phone,
                name=name,
                email=email,
                tags=tags,
                custom_attributes=custom_attr_str,
                is_opted_in=is_opted_in,
                status=status,
            )
            session.add(contact)
            session.flush()
            return contact

    if db_session is not None:
        return _execute(db_session)
    else:
        with get_db() as session:
            return _execute(session)


def get_contact_by_phone(phone_number: str, db_session: Optional[Session] = None) -> Optional[Contact]:
    """Retrieve a contact by phone number."""
    is_valid, normalized_phone, _ = validate_and_normalize_phone(phone_number)
    target_phone = normalized_phone if is_valid else phone_number

    def _execute(session: Session):
        return session.execute(
            select(Contact).where(Contact.phone_number == target_phone)
        ).scalar_one_or_none()

    if db_session is not None:
        return _execute(db_session)
    else:
        with get_db() as session:
            return _execute(session)


def get_all_contacts(limit: int = 500, db_session: Optional[Session] = None) -> List[Contact]:
    """Retrieve all contacts up to a given limit."""
    def _execute(session: Session):
        return list(session.execute(
            select(Contact).order_by(Contact.id.desc()).limit(limit)
        ).scalars().all())

    if db_session is not None:
        return _execute(db_session)
    else:
        with get_db() as session:
            return _execute(session)


def import_contacts_from_csv(
    file_path_or_df: Union[str, Path, pd.DataFrame],
    default_opt_in: bool = False,  # Never assume consent!
    consent_source: str = "csv_import",
    db_session: Optional[Session] = None,
) -> Dict[str, int]:
    """Bulk imports contacts from CSV using the validation pipeline."""
    if isinstance(file_path_or_df, (str, Path)):
        df = pd.read_csv(file_path_or_df)
    else:
        df = file_path_or_df

    validated_df, _ = process_and_validate_contacts(
        df, default_opt_in=default_opt_in, check_db=True, db_session=db_session
    )
    result = save_validated_contacts(
        validated_df, default_opt_in=default_opt_in, consent_source=consent_source, db_session=db_session
    )
    return {
        "imported": result["saved"],
        "updated": 0,
        "errors": result["errors"],
        "skipped": result["skipped"],
    }
