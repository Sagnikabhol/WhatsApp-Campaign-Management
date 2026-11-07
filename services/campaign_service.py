"""
Campaign service: Business logic for campaign creation, audience targeting,
message dispatch logging, delivery status tracking, and link click tracking.
"""

from datetime import datetime, timezone
import logging
from typing import Optional, List, Dict, Any
from sqlalchemy import select, func, desc
from sqlalchemy.orm import Session

from database.connection import get_db, RecordNotFoundError, DuplicateRecordError
from database.models import (
    Campaign,
    CampaignRecipient,
    Contact,
    MessageLog,
    MessageStatusEvent,
    LinkClickRecord,
)

logger = logging.getLogger("campaign_platform.campaigns")


def create_campaign(
    name: str,
    template_name: Optional[str] = None,
    template_language: str = "en",
    message_body: Optional[str] = None,
    target_segment: Optional[str] = None,
    scheduled_at: Optional[datetime] = None,
    status: str = "draft",
    db_session: Optional[Session] = None,
) -> Campaign:
    """Creates a new broadcast campaign record."""
    def _execute(session: Session) -> Campaign:
        campaign = Campaign(
            name=name,
            template_name=template_name,
            template_language=template_language,
            message_body=message_body,
            target_segment=target_segment,
            scheduled_at=scheduled_at,
            status=status,
        )
        session.add(campaign)
        session.flush()
        return campaign

    if db_session is not None:
        return _execute(db_session)
    else:
        with get_db() as session:
            return _execute(session)


def get_eligible_campaign_contacts(
    target_segment: Optional[str] = None,
    tags: Optional[str] = None,
    db_session: Optional[Session] = None,
) -> List[Contact]:
    """
    Retrieves all contacts eligible for WhatsApp broadcast campaigns.
    Strictly filters for active contacts with verifiable opt-in marketing consent.
    """
    def _execute(session: Session) -> List[Contact]:
        stmt = select(Contact).where(
            Contact.is_opted_in.is_(True),
            Contact.status == "active",
        )
        if tags:
            stmt = stmt.where(Contact.tags.ilike(f"%{tags.strip()}%"))
        return list(session.execute(stmt).scalars().all())

    if db_session is not None:
        return _execute(db_session)
    else:
        with get_db() as session:
            return _execute(session)


def add_recipients_to_campaign(
    campaign_id: int,
    contact_ids: List[int],
    enforce_consent: bool = True,
    db_session: Optional[Session] = None,
) -> int:
    """
    Adds a list of contacts to a campaign while skipping duplicates.
    When enforce_consent=True (default), strictly excludes non-consented
    and opted-out contacts from marketing broadcasts.
    """
    def _execute(session: Session) -> int:
        campaign = session.get(Campaign, campaign_id)
        if not campaign:
            raise RecordNotFoundError(f"Campaign with ID {campaign_id} not found.")

        # If enforcing marketing consent, filter only active opted-in contacts
        if enforce_consent:
            eligible_contact_ids = set(
                session.execute(
                    select(Contact.id).where(
                        Contact.id.in_(contact_ids),
                        Contact.is_opted_in.is_(True),
                        Contact.status == "active",
                    )
                ).scalars().all()
            )
            candidate_ids = [cid for cid in contact_ids if cid in eligible_contact_ids]
            excluded_count = len(contact_ids) - len(candidate_ids)
            if excluded_count > 0:
                logger.info(
                    f"Campaign {campaign_id}: Excluded {excluded_count} contacts without marketing consent."
                )
        else:
            candidate_ids = contact_ids

        # Find existing contact IDs for this campaign to avoid duplicates
        existing_contacts = set(
            session.execute(
                select(CampaignRecipient.contact_id).where(
                    CampaignRecipient.campaign_id == campaign_id,
                    CampaignRecipient.contact_id.in_(candidate_ids),
                )
            ).scalars().all()
        )

        added = 0
        for cid in candidate_ids:
            if cid not in existing_contacts:
                recipient = CampaignRecipient(
                    campaign_id=campaign_id,
                    contact_id=cid,
                    status="pending",
                )
                session.add(recipient)
                added += 1

        campaign.total_recipients += added
        session.flush()
        return added

    if db_session is not None:
        return _execute(db_session)
    else:
        with get_db() as session:
            return _execute(session)


def log_outbound_message(
    phone_number: str,
    contact_id: int,
    campaign_id: Optional[int] = None,
    campaign_recipient_id: Optional[int] = None,
    whatsapp_message_id: Optional[str] = None,
    message_type: str = "template",
    message_content: Optional[str] = None,
    status: str = "sent",
    sent_at: Optional[datetime] = None,
    db_session: Optional[Session] = None,
) -> MessageLog:
    """Logs an outbound WhatsApp Cloud API message."""
    def _execute(session: Session) -> MessageLog:
        now = sent_at or datetime.now(timezone.utc)
        message = MessageLog(
            whatsapp_message_id=whatsapp_message_id,
            campaign_id=campaign_id,
            contact_id=contact_id,
            campaign_recipient_id=campaign_recipient_id,
            phone_number=phone_number,
            message_type=message_type,
            message_content=message_content,
            status=status,
            sent_at=now,
        )
        session.add(message)
        session.flush()

        # Update campaign sent_count if part of campaign
        if campaign_id:
            campaign = session.get(Campaign, campaign_id)
            if campaign:
                campaign.sent_count = (campaign.sent_count or 0) + 1

        return message

    if db_session is not None:
        return _execute(db_session)
    else:
        with get_db() as session:
            return _execute(session)


def update_message_status(
    whatsapp_message_id: str,
    status: str,
    event_timestamp: Optional[datetime] = None,
    raw_payload: Optional[str] = None,
    error_code: Optional[str] = None,
    error_details: Optional[str] = None,
    db_session: Optional[Session] = None,
) -> Optional[MessageLog]:
    """Updates the message log and records an audit event in message_status_events."""
    def _execute(session: Session) -> Optional[MessageLog]:
        message = session.execute(
            select(MessageLog).where(MessageLog.whatsapp_message_id == whatsapp_message_id)
        ).scalar_one_or_none()

        if not message:
            logger.warning(f"No message found for wamid: {whatsapp_message_id}")
            return None

        event_ts = event_timestamp or datetime.now(timezone.utc)
        status_lower = status.lower()
        message.status = status_lower

        if status_lower == "delivered":
            message.delivered_at = event_ts
            if message.campaign_id:
                c = session.get(Campaign, message.campaign_id)
                if c:
                    c.delivered_count = (c.delivered_count or 0) + 1
        elif status_lower == "read":
            message.read_at = event_ts
            if message.campaign_id:
                c = session.get(Campaign, message.campaign_id)
                if c:
                    c.read_count = (c.read_count or 0) + 1
        elif status_lower == "failed":
            message.failed_at = event_ts
            message.error_code = error_code
            message.error_message = error_details
            if message.campaign_id:
                c = session.get(Campaign, message.campaign_id)
                if c:
                    c.failed_count = (c.failed_count or 0) + 1

        # Record audit event
        status_event = MessageStatusEvent(
            message_log_id=message.id,
            status=status_lower,
            event_timestamp=event_ts,
            raw_payload=raw_payload,
            error_code=error_code,
            error_details=error_details,
        )
        session.add(status_event)
        session.flush()
        return message

    if db_session is not None:
        return _execute(db_session)
    else:
        with get_db() as session:
            return _execute(session)


def record_link_click(
    original_url: str,
    campaign_id: Optional[int] = None,
    contact_id: Optional[int] = None,
    message_log_id: Optional[int] = None,
    url_code: Optional[str] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    db_session: Optional[Session] = None,
) -> LinkClickRecord:
    """Records a recipient link click event."""
    def _execute(session: Session) -> LinkClickRecord:
        click = LinkClickRecord(
            original_url=original_url,
            campaign_id=campaign_id,
            contact_id=contact_id,
            message_log_id=message_log_id,
            url_code=url_code,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        session.add(click)
        session.flush()
        return click

    if db_session is not None:
        return _execute(db_session)
    else:
        with get_db() as session:
            return _execute(session)


def get_all_campaigns(limit: int = 50, db_session: Optional[Session] = None) -> List[Campaign]:
    """Retrieves list of campaigns ordered by creation date."""
    def _execute(session: Session):
        return list(
            session.execute(
                select(Campaign).order_by(desc(Campaign.created_at)).limit(limit)
            ).scalars().all()
        )

    if db_session is not None:
        return _execute(db_session)
    else:
        with get_db() as session:
            return _execute(session)


def get_platform_kpi_summary(db_session: Optional[Session] = None) -> Dict[str, Any]:
    """
    Computes real-time KPI metrics aggregated directly from database tables.
    Returns None or empty structure if no data exists.
    """
    def _execute(session: Session) -> Dict[str, Any]:
        total_contacts = session.scalar(select(func.count(Contact.id))) or 0
        eligible_contacts = session.scalar(
            select(func.count(Contact.id)).where(
                Contact.is_opted_in.is_(True),
                Contact.status == "active",
            )
        ) or 0
        total_campaigns = session.scalar(select(func.count(Campaign.id))) or 0
        messages_sent = session.scalar(select(func.count(MessageLog.id))) or 0
        delivered_messages = session.scalar(
            select(func.count(MessageLog.id)).where(MessageLog.delivered_at.is_not(None))
        ) or 0
        read_messages = session.scalar(
            select(func.count(MessageLog.id)).where(MessageLog.read_at.is_not(None))
        ) or 0
        failed_messages = session.scalar(
            select(func.count(MessageLog.id)).where(MessageLog.status == "failed")
        ) or 0
        total_clicks = session.scalar(select(func.count(LinkClickRecord.id))) or 0

        # Performance percentages
        delivery_rate = round((delivered_messages / messages_sent * 100), 1) if messages_sent > 0 else 0.0
        read_rate = round((read_messages / delivered_messages * 100), 1) if delivered_messages > 0 else 0.0
        failure_rate = round((failed_messages / messages_sent * 100), 1) if messages_sent > 0 else 0.0
        ctr_rate = round((total_clicks / delivered_messages * 100), 1) if delivered_messages > 0 else 0.0

        return {
            "total_contacts": total_contacts,
            "eligible_contacts": eligible_contacts,
            "total_campaigns": total_campaigns,
            "messages_sent": messages_sent,
            "delivered_messages": delivered_messages,
            "read_messages": read_messages,
            "failed_messages": failed_messages,
            "total_clicks": total_clicks,
            "delivery_rate": delivery_rate,
            "read_rate": read_rate,
            "failure_rate": failure_rate,
            "ctr_rate": ctr_rate,
        }

    if db_session is not None:
        return _execute(db_session)
    else:
        with get_db() as session:
            return _execute(session)
