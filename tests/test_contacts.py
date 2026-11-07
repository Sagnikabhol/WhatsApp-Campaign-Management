"""
Unit tests for contact parsing, international phone number validation, E.164 normalization,
duplicate detection (in-file and database), CSV import pipeline, SQLite persistence,
and WhatsApp marketing consent management (opt-in, opt-out, and audit trails).
"""

from pathlib import Path
import pandas as pd
import pytest
from sqlalchemy.orm import Session

from database.connection import create_db_engine, init_db
from database.models import Contact, ConsentRecord
from services.contact_service import (
    validate_and_normalize_phone,
    normalize_phone_number,
    detect_contact_columns,
    process_and_validate_contacts,
    save_validated_contacts,
    get_contacts_as_df,
    delete_contact,
    add_or_update_contact,
    record_consent,
    record_opt_in,
    record_opt_out,
    record_opt_out_by_phone,
    get_consent_history,
    get_contact_by_phone,
    get_all_contacts,
    count_contacts,
    import_contacts_from_csv,
)


@pytest.fixture
def contact_session():
    """Provides a fresh isolated in-memory test database session."""
    engine = create_db_engine("sqlite:///:memory:")
    init_db(engine)
    with Session(bind=engine) as session:
        yield session
    engine.dispose()


# ==============================================================================
# 1. PHONE NUMBER VALIDATION & NORMALIZATION TESTS
# ==============================================================================

def test_validate_and_normalize_valid_international_formats():
    valid, norm, msg = validate_and_normalize_phone("+1 (415) 555-2671")
    assert valid is True
    assert norm == "+14155552671"

    valid, norm, msg = validate_and_normalize_phone("+91 98765-43210")
    assert valid is True
    assert norm == "+919876543210"

    valid, norm, msg = validate_and_normalize_phone("+44 7911 123456")
    assert valid is True
    assert norm == "+447911123456"

    valid, norm, msg = validate_and_normalize_phone("0014155552671")
    assert valid is True
    assert norm == "+14155552671"

    valid, norm, msg = validate_and_normalize_phone("919876543210")
    assert valid is True
    assert norm == "+919876543210"

    valid, norm, msg = validate_and_normalize_phone("4155552671", default_country_code="1")
    assert valid is True
    assert norm == "+14155552671"


def test_validate_and_normalize_invalid_formats():
    valid, _, _ = validate_and_normalize_phone("")
    assert valid is False

    valid, _, _ = validate_and_normalize_phone(None)
    assert valid is False

    valid, _, msg = validate_and_normalize_phone("+1415555ABCD")
    assert valid is False
    assert "non-numeric" in msg.lower()

    valid, _, msg = validate_and_normalize_phone("+12345")
    assert valid is False
    assert "too short" in msg.lower()

    valid, _, msg = validate_and_normalize_phone("+1234567890123456789")
    assert valid is False
    assert "too long" in msg.lower()

    valid, _, msg = validate_and_normalize_phone("+0123456789")
    assert valid is False
    assert "cannot start with 0" in msg.lower()


# ==============================================================================
# 2. COLUMN DETECTION TESTS
# ==============================================================================

def test_detect_contact_columns():
    cols1 = ["Full Name", "Mobile Number", "Email Address", "Tags", "Marketing Consent"]
    detected1 = detect_contact_columns(cols1)
    assert detected1["name"] == "Full Name"
    assert detected1["phone"] == "Mobile Number"
    assert detected1["email"] == "Email Address"
    assert detected1["tags"] == "Tags"
    assert detected1["consent"] == "Marketing Consent"

    cols2 = ["contact_name", "whatsapp", "variable", "opt_in"]
    detected2 = detect_contact_columns(cols2)
    assert detected2["name"] == "contact_name"
    assert detected2["phone"] == "whatsapp"
    assert detected2["custom"] == "variable"
    assert detected2["consent"] == "opt_in"


# ==============================================================================
# 3. CSV VALIDATION & DUPLICATE DETECTION TESTS
# ==============================================================================

def test_process_and_validate_contacts_duplicates_and_invalids(contact_session):
    existing_contact = Contact(
        phone_number="+14155550001",
        name="Existing DB Contact",
        is_opted_in=True,
    )
    contact_session.add(existing_contact)
    contact_session.commit()

    raw_data = {
        "Name": ["Alice New", "Bad Number", "Alice Dup", "Existing In DB"],
        "Phone": [
            "+1 (415) 555-1111",
            "invalid_phone_123",
            "+1 415 555 1111",      # Duplicate in file
            "+1-415-555-0001",      # Duplicate in DB
        ],
        "Email": ["alice@example.com", "bad@example.com", "alice2@example.com", "exist@example.com"],
    }
    df = pd.DataFrame(raw_data)

    validated_df, counts = process_and_validate_contacts(
        df,
        name_col="Name",
        phone_col="Phone",
        default_opt_in=True,
        check_db=True,
        db_session=contact_session,
    )

    assert counts["total"] == 4
    assert counts["valid"] == 1
    assert counts["invalid"] == 1
    assert counts["duplicate_file"] == 1
    assert counts["duplicate_db"] == 1
    assert counts["duplicate_total"] == 2

    statuses = list(validated_df["Validation Status"])
    assert statuses[0] == "VALID"
    assert statuses[1] == "INVALID_FORMAT"
    assert statuses[2] == "DUPLICATE_IN_FILE"
    assert statuses[3] == "ALREADY_IN_DATABASE"


# ==============================================================================
# 4. MARKETING CONSENT COMPLIANCE & POLICY TESTS
# ==============================================================================

def test_never_assume_consent_on_upload(contact_session):
    """Verify that uploading contacts without explicit opt-in defaults to NO_CONSENT / Ineligible."""
    raw_data = {
        "Name": ["Unconsented User 1", "Unconsented User 2"],
        "Phone": ["+14155558801", "+14155558802"],
    }
    df = pd.DataFrame(raw_data)

    # Process with default_opt_in=False (default behavior)
    validated_df, counts = process_and_validate_contacts(
        df,
        default_opt_in=False,
        check_db=True,
        db_session=contact_session,
    )

    # All valid numbers must be flagged as Ineligible for campaigns
    assert counts["valid"] == 2
    assert counts["eligible"] == 0
    assert counts["ineligible"] == 2
    assert validated_df.iloc[0]["Consent Status"] == "NO_CONSENT"
    assert "Ineligible" in validated_df.iloc[0]["Campaign Eligibility"]

    # Save to database
    save_res = save_validated_contacts(
        validated_df,
        default_opt_in=False,
        consent_source="csv_import_no_consent",
        db_session=contact_session,
    )
    contact_session.commit()

    assert save_res["saved"] == 2

    # Verify contact is NOT opted in
    c = get_contact_by_phone("+14155558801", db_session=contact_session)
    assert c is not None
    assert c.is_opted_in is False
    assert len(c.consent_records) == 1
    assert c.consent_records[0].status == "NO_CONSENT"

    # Verify database counts
    db_counts = count_contacts(db_session=contact_session)
    assert db_counts["total"] == 2
    assert db_counts["eligible"] == 0
    assert db_counts["ineligible"] == 2
    assert db_counts["pending_consent"] == 2


def test_explicit_consent_column_in_csv(contact_session):
    """Verify per-row consent parsing when CSV has a consent column."""
    raw_data = {
        "Name": ["Opted In User", "Opted Out User", "Active User"],
        "Phone": ["+14155557701", "+14155557702", "+14155557703"],
        "Consent": ["yes", "no", "true"],
    }
    df = pd.DataFrame(raw_data)

    validated_df, counts = process_and_validate_contacts(
        df,
        consent_col="Consent",
        check_db=True,
        db_session=contact_session,
    )

    assert counts["valid"] == 3
    assert counts["eligible"] == 2
    assert counts["ineligible"] == 1

    save_validated_contacts(validated_df, db_session=contact_session)
    contact_session.commit()

    c1 = get_contact_by_phone("+14155557701", db_session=contact_session)
    c2 = get_contact_by_phone("+14155557702", db_session=contact_session)
    c3 = get_contact_by_phone("+14155557703", db_session=contact_session)

    assert c1.is_opted_in is True
    assert c2.is_opted_in is False
    assert c3.is_opted_in is True


def test_record_opt_out_and_opt_in_lifecycle(contact_session):
    """Test recording opt-out requests (STOP) and subsequent opt-in with audit log."""
    contact = add_or_update_contact(
        phone_number="+14155556601",
        name="Lifecycle Consent User",
        is_opted_in=True,
        db_session=contact_session,
    )
    contact_session.commit()
    assert contact.is_opted_in is True

    # 1. Record Opt-out request
    opt_out_record = record_opt_out(
        contact_id=contact.id,
        source="whatsapp_stop_keyword",
        proof="Recipient sent STOP",
        notes="Unsubscribed from marketing",
        db_session=contact_session,
    )
    contact_session.commit()

    assert contact.is_opted_in is False
    assert contact.status == "unsubscribed"
    assert opt_out_record.status == "OPT_OUT"
    assert opt_out_record.source == "whatsapp_stop_keyword"

    # 2. Record Opt-out by phone wrapper
    opt_out_phone = record_opt_out_by_phone(
        "+14155556601",
        source="customer_support",
        notes="Verified via phone agent",
        db_session=contact_session,
    )
    contact_session.commit()
    assert opt_out_phone is not None

    # 3. User re-consents later
    opt_in_record = record_opt_in(
        contact_id=contact.id,
        source="web_portal_resubscribe",
        proof="IP 127.0.0.1 Form #909",
        notes="Re-subscribed voluntarily",
        db_session=contact_session,
    )
    contact_session.commit()

    assert contact.is_opted_in is True
    assert contact.status == "active"
    assert opt_in_record.status == "OPT_IN"


def test_consent_history_audit_trail(contact_session):
    """Verify that multiple consent changes are maintained in chronological order."""
    contact = add_or_update_contact(
        phone_number="+14155554401",
        name="Audit History User",
        is_opted_in=False,
        db_session=contact_session,
    )
    contact_session.commit()

    # Event 1: Initial consent
    record_opt_in(contact.id, source="web_form", proof="Form #1", db_session=contact_session)
    contact_session.commit()

    # Event 2: Opt-out via STOP
    record_opt_out(contact.id, source="whatsapp_stop", proof="Msg #2", db_session=contact_session)
    contact_session.commit()

    # Event 3: Re-subscribe
    record_opt_in(contact.id, source="checkout_optin", proof="Order #3", db_session=contact_session)
    contact_session.commit()

    history = get_consent_history(contact.id, db_session=contact_session)
    assert len(history) == 3
    # Ordered descending by timestamp
    assert history[0]["status"] == "OPT_IN"
    assert history[0]["source"] == "checkout_optin"
    assert history[1]["status"] == "OPT_OUT"
    assert history[1]["source"] == "whatsapp_stop"
    assert history[2]["status"] == "OPT_IN"
    assert history[2]["source"] == "web_form"


def test_filter_contacts_by_consent_status(contact_session):
    """Test filtering contacts directory by consent status."""
    # Contact 1: Eligible (Opted In)
    c1 = add_or_update_contact(phone_number="+10000000001", name="Eligible 1", is_opted_in=True, db_session=contact_session)
    record_opt_in(c1.id, source="web", db_session=contact_session)

    # Contact 2: Opted Out
    c2 = add_or_update_contact(phone_number="+10000000002", name="OptedOut 2", is_opted_in=True, db_session=contact_session)
    record_opt_out(c2.id, source="stop", db_session=contact_session)

    # Contact 3: No Consent (Pending)
    c3 = add_or_update_contact(phone_number="+10000000003", name="NoConsent 3", is_opted_in=False, db_session=contact_session)

    contact_session.commit()

    # Filter: Eligible
    df_eligible = get_contacts_as_df(consent_filter="Eligible (Opted In)", db_session=contact_session)
    assert len(df_eligible) == 1
    assert df_eligible.iloc[0]["Mobile Number"] == "+10000000001"

    # Filter: Ineligible
    df_ineligible = get_contacts_as_df(consent_filter="Ineligible (No Consent / Opted Out)", db_session=contact_session)
    assert len(df_ineligible) == 2

    # Filter: Opted Out
    df_opted_out = get_contacts_as_df(consent_filter="Explicitly Opted Out", db_session=contact_session)
    assert len(df_opted_out) == 1
    assert df_opted_out.iloc[0]["Mobile Number"] == "+10000000002"

    # Filter: Pending Consent
    df_pending = get_contacts_as_df(consent_filter="Pending Consent", db_session=contact_session)
    assert len(df_pending) == 1
    assert df_pending.iloc[0]["Mobile Number"] == "+10000000003"


def test_display_eligible_and_ineligible_counts(contact_session):
    """Verify audience KPI metrics breakdown."""
    c1 = add_or_update_contact(phone_number="+12220000001", is_opted_in=True, db_session=contact_session)
    c2 = add_or_update_contact(phone_number="+12220000002", is_opted_in=False, db_session=contact_session)
    c3 = add_or_update_contact(phone_number="+12220000003", is_opted_in=True, db_session=contact_session)
    record_opt_out(c3.id, db_session=contact_session)
    contact_session.commit()

    counts = count_contacts(db_session=contact_session)
    assert counts["total"] == 3
    assert counts["eligible"] == 1
    assert counts["ineligible"] == 2
    assert counts["opted_out"] == 1
    assert counts["pending_consent"] == 1
    assert counts["opt_in_rate"] == 33.3


# ==============================================================================
# 5. STORAGE & SAMPLE IMPORT INTEGRATION
# ==============================================================================

def test_sample_contacts_csv_import_with_consent(contact_session):
    sample_csv = Path(__file__).resolve().parent.parent / "data" / "sample_contacts.csv"
    assert sample_csv.exists()

    df = pd.read_csv(sample_csv)
    validated_df, counts = process_and_validate_contacts(
        df, default_opt_in=True, check_db=True, db_session=contact_session
    )

    assert counts["total"] == 3
    assert counts["valid"] == 3
    assert counts["eligible"] == 3

    res = save_validated_contacts(
        validated_df, default_opt_in=True, consent_source="sample_csv", db_session=contact_session
    )
    contact_session.commit()

    assert res["saved"] == 3
    db_counts = count_contacts(db_session=contact_session)
    assert db_counts["total"] == 3
    assert db_counts["eligible"] == 3
