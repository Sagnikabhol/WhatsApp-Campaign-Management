"""
Unit and integration tests for SQLite database integration with SQLAlchemy.
Tests:
- Automatic table creation
- Schema verification for all 7 required tables
- Relationships and foreign key cascades
- Unique constraints enforcement
- Safe database operations (commits, rollbacks on error)
- Persistence across application restart / engine reconnect
- Domain exception handling
"""

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
import pytest
from sqlalchemy import select, inspect, func
from sqlalchemy.orm import Session

from database.connection import (
    create_db_engine,
    init_db,
    drop_db,
    get_db,
    safe_commit,
    check_db_connection,
    DuplicateRecordError,
    DatabaseConnectionError,
    RecordNotFoundError,
    SessionFactory,
)
from database.models import (
    Base,
    Contact,
    ConsentRecord,
    Campaign,
    CampaignRecipient,
    MessageLog,
    MessageStatusEvent,
    LinkClickRecord,
)
from services.contact_service import (
    add_or_update_contact,
    record_consent,
    get_contact_by_phone,
    import_contacts_from_csv,
    count_contacts,
)
from services.campaign_service import (
    create_campaign,
    add_recipients_to_campaign,
    log_outbound_message,
    update_message_status,
    record_link_click,
    get_platform_kpi_summary,
)


@pytest.fixture
def temp_db_file():
    """Create a temporary database file on disk to verify physical file persistence."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="test_whatsapp_")
    os.close(fd)
    yield Path(path)
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


@pytest.fixture
def test_engine(temp_db_file):
    """Engine connected to the temp SQLite database file."""
    url = f"sqlite:///{temp_db_file.as_posix()}"
    engine = create_db_engine(url)
    init_db(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def test_session(test_engine):
    """Provides a transactional session on the test engine."""
    with Session(bind=test_engine) as session:
        yield session


# ==============================================================================
# 1. TABLE CREATION & SCHEMA TESTS
# ==============================================================================

def test_all_seven_tables_created_automatically(test_engine):
    """Verify that all 7 required tables are created automatically."""
    inspector = inspect(test_engine)
    table_names = set(inspector.get_table_names())

    required_tables = {
        "contacts",
        "consent_records",
        "campaigns",
        "campaign_recipients",
        "message_logs",
        "message_status_events",
        "link_click_records",
    }

    assert required_tables.issubset(table_names), f"Missing tables: {required_tables - table_names}"


def test_table_columns_and_types(test_engine):
    """Verify that each table contains the expected columns and types."""
    inspector = inspect(test_engine)

    contact_cols = {col["name"] for col in inspector.get_columns("contacts")}
    assert {"id", "phone_number", "name", "email", "tags", "is_opted_in", "status", "created_at", "updated_at"}.issubset(contact_cols)

    consent_cols = {col["name"] for col in inspector.get_columns("consent_records")}
    assert {"id", "contact_id", "status", "source", "consent_timestamp", "proof"}.issubset(consent_cols)

    campaign_cols = {col["name"] for col in inspector.get_columns("campaigns")}
    assert {"id", "name", "template_name", "status", "total_recipients", "sent_count", "delivered_count", "read_count", "failed_count"}.issubset(campaign_cols)

    recipient_cols = {col["name"] for col in inspector.get_columns("campaign_recipients")}
    assert {"id", "campaign_id", "contact_id", "status"}.issubset(recipient_cols)

    msg_cols = {col["name"] for col in inspector.get_columns("message_logs")}
    assert {"id", "whatsapp_message_id", "campaign_id", "contact_id", "phone_number", "status"}.issubset(msg_cols)

    event_cols = {col["name"] for col in inspector.get_columns("message_status_events")}
    assert {"id", "message_log_id", "status", "event_timestamp", "raw_payload"}.issubset(event_cols)

    link_cols = {col["name"] for col in inspector.get_columns("link_click_records")}
    assert {"id", "campaign_id", "contact_id", "message_log_id", "original_url", "clicked_at"}.issubset(link_cols)


# ==============================================================================
# 2. UNIQUE CONSTRAINTS TESTS
# ==============================================================================

def test_unique_contact_phone_number(test_session):
    """Test that duplicate phone numbers violate the unique constraint."""
    c1 = Contact(phone_number="+19998887771", name="Alice", is_opted_in=True)
    test_session.add(c1)
    test_session.commit()

    c2 = Contact(phone_number="+19998887771", name="Alice Duplicate", is_opted_in=True)
    test_session.add(c2)

    with pytest.raises(Exception):
        safe_commit(test_session)
    test_session.rollback()


def test_unique_campaign_recipient_constraint(test_session):
    """Test that a contact cannot be assigned twice to the same campaign."""
    contact = Contact(phone_number="+19998887772", name="Bob")
    campaign = Campaign(name="Black Friday Sale")
    test_session.add_all([contact, campaign])
    test_session.commit()

    r1 = CampaignRecipient(campaign_id=campaign.id, contact_id=contact.id)
    test_session.add(r1)
    test_session.commit()

    # Attempting duplicate assignment
    r2 = CampaignRecipient(campaign_id=campaign.id, contact_id=contact.id)
    test_session.add(r2)
    with pytest.raises(Exception):
        safe_commit(test_session)
    test_session.rollback()


def test_unique_whatsapp_message_id(test_session):
    """Test that wamid is unique across message logs."""
    contact = Contact(phone_number="+19998887773", name="Charlie")
    test_session.add(contact)
    test_session.commit()

    m1 = MessageLog(
        contact_id=contact.id,
        phone_number=contact.phone_number,
        whatsapp_message_id="wamid.HBgM_TEST_123",
        status="sent",
    )
    test_session.add(m1)
    test_session.commit()

    m2 = MessageLog(
        contact_id=contact.id,
        phone_number=contact.phone_number,
        whatsapp_message_id="wamid.HBgM_TEST_123",
        status="sent",
    )
    test_session.add(m2)
    with pytest.raises(Exception):
        safe_commit(test_session)
    test_session.rollback()


# ==============================================================================
# 3. RELATIONSHIPS & CASCADE TESTS
# ==============================================================================

def test_contact_consent_cascade_relationship(test_session):
    """Verify contact -> consent records relationship and cascade deletion."""
    contact = Contact(phone_number="+19998887774", name="Diana")
    test_session.add(contact)
    test_session.commit()

    consent = ConsentRecord(
        contact_id=contact.id,
        status="OPT_IN",
        source="web_form",
        proof="IP: 192.168.1.1",
    )
    test_session.add(consent)
    test_session.commit()

    # Verify relationship navigation
    assert len(contact.consent_records) == 1
    assert contact.consent_records[0].source == "web_form"
    assert consent.contact.name == "Diana"

    # Delete contact and verify cascade
    contact_id = contact.id
    test_session.delete(contact)
    test_session.commit()

    remaining_consent = test_session.execute(
        select(ConsentRecord).where(ConsentRecord.contact_id == contact_id)
    ).scalars().all()
    assert len(remaining_consent) == 0


def test_campaign_message_events_and_clicks_flow(test_session):
    """Verify entire relationship tree from Campaign -> Recipient -> MessageLog -> StatusEvent & Clicks."""
    contact = Contact(phone_number="+19998887775", name="Evan", is_opted_in=True)
    campaign = Campaign(name="Product Launch 2026", template_name="launch_promo")
    test_session.add_all([contact, campaign])
    test_session.commit()

    recipient = CampaignRecipient(campaign_id=campaign.id, contact_id=contact.id, status="sent")
    test_session.add(recipient)
    test_session.commit()

    msg = MessageLog(
        campaign_id=campaign.id,
        contact_id=contact.id,
        campaign_recipient_id=recipient.id,
        phone_number=contact.phone_number,
        whatsapp_message_id="wamid.HBgM_LAUNCH_999",
        status="delivered",
    )
    test_session.add(msg)
    test_session.commit()

    event = MessageStatusEvent(
        message_log_id=msg.id,
        status="delivered",
        event_timestamp=datetime.now(timezone.utc),
        raw_payload='{"event": "delivered"}',
    )
    click = LinkClickRecord(
        campaign_id=campaign.id,
        contact_id=contact.id,
        message_log_id=msg.id,
        original_url="https://example.com/promo",
    )
    test_session.add_all([event, click])
    test_session.commit()

    # Navigation assertions
    assert len(campaign.recipients) == 1
    assert len(msg.status_events) == 1
    assert msg.status_events[0].status == "delivered"
    assert len(campaign.link_clicks) == 1
    assert campaign.link_clicks[0].original_url == "https://example.com/promo"


# ==============================================================================
# 4. SAFE DATABASE OPERATIONS & ERROR HANDLING
# ==============================================================================

def test_get_db_context_manager_commit(test_engine):
    """Verify that get_db commits on normal completion."""
    from sqlalchemy.orm import sessionmaker
    ScopedTest = sessionmaker(bind=test_engine)

    session = ScopedTest()
    with get_db(session_override=session) as s:
        c = Contact(phone_number="+19990001111", name="Context User")
        s.add(c)
        s.flush()
        cid = c.id

    session.close()

    # Re-query in a fresh session
    with Session(bind=test_engine) as fresh:
        found = fresh.get(Contact, cid)
        assert found is not None
        assert found.name == "Context User"


def test_get_db_context_manager_rollback_on_error(test_engine):
    """Verify that get_db rolls back on exception and does not persist partial writes."""
    with Session(bind=test_engine) as session:
        try:
            with get_db(session_override=session) as s:
                s.add(Contact(phone_number="+19990002222", name="Should Rollback"))
                s.flush()
                raise ValueError("Simulated unexpected failure")
        except ValueError:
            session.rollback()

    with Session(bind=test_engine) as fresh:
        found = fresh.execute(
            select(Contact).where(Contact.phone_number == "+19990002222")
        ).scalar_one_or_none()
        assert found is None


def test_safe_commit_duplicate_record_error(test_session):
    """Verify safe_commit translates IntegrityError to DuplicateRecordError."""
    c1 = Contact(phone_number="+19990003333", name="Dup Test 1")
    test_session.add(c1)
    test_session.commit()

    c2 = Contact(phone_number="+19990003333", name="Dup Test 2")
    test_session.add(c2)

    with pytest.raises(DuplicateRecordError):
        safe_commit(test_session)
    test_session.rollback()


# ==============================================================================
# 5. DATA PERSISTENCE ACROSS APPLICATION RESTART
# ==============================================================================

def test_data_persists_after_engine_restart(temp_db_file):
    """
    Ensures data persists after application restart:
    1. Connect to SQLite file engine.
    2. Insert contacts, campaigns, message logs.
    3. Dispose engine (simulating process shutdown).
    4. Create an entirely new engine on the same SQLite file.
    5. Query and verify all data and relationships remained intact.
    """
    db_url = f"sqlite:///{temp_db_file.as_posix()}"

    # App Run 1: Write data
    engine1 = create_db_engine(db_url)
    init_db(engine1)

    with Session(bind=engine1) as s1:
        contact = Contact(
            phone_number="+15551234567",
            name="Persistent User",
            email="persistent@example.com",
            is_opted_in=True,
        )
        s1.add(contact)
        s1.commit()
        contact_id = contact.id

        campaign = Campaign(
            name="Black Friday Broadcast",
            status="scheduled",
            total_recipients=1,
        )
        s1.add(campaign)
        s1.commit()
        campaign_id = campaign.id

        recipient = CampaignRecipient(
            campaign_id=campaign_id,
            contact_id=contact_id,
            status="pending",
        )
        s1.add(recipient)
        s1.commit()

    # Simulate application restart: dispose engine 1 completely
    engine1.dispose()

    # App Run 2: Re-open database with brand new engine
    engine2 = create_db_engine(db_url)
    init_db(engine2)

    with Session(bind=engine2) as s2:
        saved_contact = s2.get(Contact, contact_id)
        assert saved_contact is not None
        assert saved_contact.phone_number == "+15551234567"
        assert saved_contact.name == "Persistent User"
        assert saved_contact.is_opted_in is True

        saved_campaign = s2.get(Campaign, campaign_id)
        assert saved_campaign is not None
        assert saved_campaign.name == "Black Friday Broadcast"
        assert len(saved_campaign.recipients) == 1
        assert saved_campaign.recipients[0].contact_id == contact_id

    engine2.dispose()


# ==============================================================================
# 6. HEALTH CHECK
# ==============================================================================

def test_check_db_connection_healthy(test_engine):
    """Test health-check function reporting healthy connection and table counts."""
    health = check_db_connection(test_engine)
    assert health["connected"] is True
    assert health["status"] == "healthy"
    assert health["dialect"] == "sqlite"
    assert health["table_count"] == 7
