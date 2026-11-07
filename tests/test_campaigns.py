"""
Unit tests for campaign creation, audience targeting, message logging, and status tracking.
"""

from datetime import datetime, timezone
import pytest
from sqlalchemy.orm import Session

from database.connection import create_db_engine, init_db
from database.models import Campaign, Contact
from services.contact_service import add_or_update_contact
from services.campaign_service import (
    create_campaign,
    add_recipients_to_campaign,
    log_outbound_message,
    update_message_status,
    record_link_click,
    get_all_campaigns,
    get_platform_kpi_summary,
)


@pytest.fixture
def campaign_session():
    """Provides a fresh isolated in-memory test database session."""
    engine = create_db_engine("sqlite:///:memory:")
    init_db(engine)
    with Session(bind=engine) as session:
        yield session
    engine.dispose()


def test_create_and_query_campaign(campaign_session):
    campaign = create_campaign(
        name="Summer Festive Broadcast",
        template_name="summer_promo_v1",
        target_segment="VIP Segment",
        status="draft",
        db_session=campaign_session,
    )
    campaign_session.commit()

    assert campaign.id is not None
    assert campaign.name == "Summer Festive Broadcast"

    campaigns = get_all_campaigns(db_session=campaign_session)
    assert len(campaigns) == 1
    assert campaigns[0].template_name == "summer_promo_v1"


def test_add_recipients_to_campaign(campaign_session):
    c1 = add_or_update_contact(phone_number="+11111111111", name="User 1", is_opted_in=True, db_session=campaign_session)
    c2 = add_or_update_contact(phone_number="+22222222222", name="User 2", is_opted_in=True, db_session=campaign_session)
    campaign_session.commit()

    campaign = create_campaign(name="Targeted Campaign", db_session=campaign_session)
    campaign_session.commit()

    # Add recipients
    added = add_recipients_to_campaign(
        campaign_id=campaign.id,
        contact_ids=[c1.id, c2.id],
        db_session=campaign_session,
    )
    campaign_session.commit()

    assert added == 2
    assert campaign.total_recipients == 2

    # Attempt to re-add same recipients; duplicates should be ignored safely
    added_again = add_recipients_to_campaign(
        campaign_id=campaign.id,
        contact_ids=[c1.id, c2.id],
        db_session=campaign_session,
    )
    campaign_session.commit()
    assert added_again == 0
    assert campaign.total_recipients == 2


def test_exclude_non_consented_and_opted_out_contacts_from_campaign(campaign_session):
    """Verify that marketing campaigns strictly exclude non-consented and opted-out contacts."""
    from services.contact_service import record_opt_out

    # 1. Eligible Contact
    c_eligible = add_or_update_contact(
        phone_number="+18880000001", name="Consented User", is_opted_in=True, db_session=campaign_session
    )
    # 2. Ineligible: No consent
    c_no_consent = add_or_update_contact(
        phone_number="+18880000002", name="No Consent User", is_opted_in=False, db_session=campaign_session
    )
    # 3. Ineligible: Opted out
    c_opted_out = add_or_update_contact(
        phone_number="+18880000003", name="Opted Out User", is_opted_in=True, db_session=campaign_session
    )
    record_opt_out(c_opted_out.id, db_session=campaign_session)
    campaign_session.commit()

    campaign = create_campaign(name="Consent Restricted Broadcast", db_session=campaign_session)
    campaign_session.commit()

    # Attempt to add all 3 contacts with consent enforcement
    added = add_recipients_to_campaign(
        campaign_id=campaign.id,
        contact_ids=[c_eligible.id, c_no_consent.id, c_opted_out.id],
        enforce_consent=True,
        db_session=campaign_session,
    )
    campaign_session.commit()

    # Only the eligible contact should be added
    assert added == 1
    assert campaign.total_recipients == 1
    assert len(campaign.recipients) == 1
    assert campaign.recipients[0].contact_id == c_eligible.id


def test_message_lifecycle_and_status_updates(campaign_session):
    contact = add_or_update_contact(
        phone_number="+33333333333",
        name="Lifecycle User",
        is_opted_in=True,
        db_session=campaign_session,
    )
    campaign = create_campaign(name="Lifecycle Campaign", db_session=campaign_session)
    campaign_session.commit()

    # 1. Log outbound message
    msg = log_outbound_message(
        phone_number=contact.phone_number,
        contact_id=contact.id,
        campaign_id=campaign.id,
        whatsapp_message_id="wamid.HBgM_TEST_LIFECYCLE",
        message_content="Hello from test!",
        status="sent",
        db_session=campaign_session,
    )
    campaign_session.commit()

    assert msg.id is not None
    assert msg.status == "sent"
    assert campaign.sent_count == 1

    # 2. Update to delivered via webhook
    delivered_msg = update_message_status(
        whatsapp_message_id="wamid.HBgM_TEST_LIFECYCLE",
        status="delivered",
        event_timestamp=datetime.now(timezone.utc),
        raw_payload='{"status": "delivered"}',
        db_session=campaign_session,
    )
    campaign_session.commit()

    assert delivered_msg.status == "delivered"
    assert delivered_msg.delivered_at is not None
    assert campaign.delivered_count == 1

    # 3. Update to read
    read_msg = update_message_status(
        whatsapp_message_id="wamid.HBgM_TEST_LIFECYCLE",
        status="read",
        event_timestamp=datetime.now(timezone.utc),
        raw_payload='{"status": "read"}',
        db_session=campaign_session,
    )
    campaign_session.commit()

    assert read_msg.status == "read"
    assert read_msg.read_at is not None
    assert campaign.read_count == 1

    # 4. Record link click
    click = record_link_click(
        original_url="https://example.com/shop",
        campaign_id=campaign.id,
        contact_id=contact.id,
        message_log_id=msg.id,
        url_code="shop123",
        db_session=campaign_session,
    )
    campaign_session.commit()

    assert click.id is not None
    assert click.url_code == "shop123"

    # 5. Check KPI summary
    summary = get_platform_kpi_summary(db_session=campaign_session)
    assert summary["messages_sent"] == 1
    assert summary["delivered_messages"] == 1
    assert summary["read_messages"] == 1
    assert summary["total_clicks"] == 1
    assert summary["delivery_rate"] == 100.0
    assert summary["read_rate"] == 100.0
    assert summary["ctr_rate"] == 100.0
