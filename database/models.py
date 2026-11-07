"""
Database models: ORM schema definitions for WhatsApp Campaign Platform.

Tables:
1. Contact - Subscriber and audience information
2. ConsentRecord - Opt-in / Opt-out compliance and audit log
3. Campaign - Broadcast campaigns and message templates
4. CampaignRecipient - Targeting / junction records linking campaigns to contacts
5. MessageLog - Outbound messages dispatched via WhatsApp Cloud API
6. MessageStatusEvent - Granular webhook status lifecycle (sent, delivered, read, failed)
7. LinkClickRecord - Click-through tracking for campaign links
"""

from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    Boolean,
    DateTime,
    ForeignKey,
    UniqueConstraint,
    Index,
    MetaData,
    func,
)
from sqlalchemy.orm import declarative_base, relationship

# Standard naming convention for constraints to support smooth migrations (SQLite & MySQL)
convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=convention)
Base = declarative_base(metadata=metadata)


# ==============================================================================
# 1. CONTACTS
# ==============================================================================

class Contact(Base):
    """
    Represents an audience subscriber / contact.
    Phone number is validated and stored in E.164 format with unique constraint.
    """
    __tablename__ = "contacts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    phone_number = Column(String(32), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=True)
    email = Column(String(255), nullable=True, index=True)
    tags = Column(String(500), nullable=True)  # Comma-separated or tag list string (e.g., "vip,lead")
    custom_attributes = Column(Text, nullable=True)  # JSON-encoded key-value attributes
    is_opted_in = Column(Boolean, default=False, nullable=False, index=True)
    status = Column(String(50), default="active", nullable=False, index=True)  # active, unsubscribed, bounced, invalid
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    consent_records = relationship(
        "ConsentRecord",
        back_populates="contact",
        cascade="all, delete-orphan",
        order_by="desc(ConsentRecord.id)",
        lazy="selectin",
    )
    campaign_recipients = relationship(
        "CampaignRecipient",
        back_populates="contact",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    message_logs = relationship(
        "MessageLog",
        back_populates="contact",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    link_clicks = relationship(
        "LinkClickRecord",
        back_populates="contact",
        lazy="selectin",
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "phone_number": self.phone_number,
            "name": self.name,
            "email": self.email,
            "tags": self.tags,
            "custom_attributes": self.custom_attributes,
            "is_opted_in": self.is_opted_in,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:
        return f"<Contact(id={self.id}, phone={self.phone_number!r}, name={self.name!r}, opt_in={self.is_opted_in})>"


# ==============================================================================
# 2. CONSENT RECORDS
# ==============================================================================

class ConsentRecord(Base):
    """
    Auditable opt-in / opt-out verification record for regulatory compliance (GDPR, TCPA, Meta).
    """
    __tablename__ = "consent_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    contact_id = Column(
        Integer,
        ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status = Column(String(50), nullable=False, index=True)  # OPT_IN, OPT_OUT, REVOKED, PENDING
    source = Column(String(100), nullable=True)  # csv_import, web_form, qr_code, whatsapp_keyword, manual
    consent_timestamp = Column(DateTime, server_default=func.now(), nullable=False)
    proof = Column(Text, nullable=True)  # IP address, form submission payload, or chat message proof
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    # Relationships
    contact = relationship("Contact", back_populates="consent_records")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "contact_id": self.contact_id,
            "status": self.status,
            "source": self.source,
            "consent_timestamp": self.consent_timestamp.isoformat() if self.consent_timestamp else None,
            "proof": self.proof,
            "notes": self.notes,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:
        return f"<ConsentRecord(id={self.id}, contact_id={self.contact_id}, status={self.status!r}, source={self.source!r})>"


# ==============================================================================
# 3. CAMPAIGNS
# ==============================================================================

class Campaign(Base):
    """
    Represents a marketing or broadcast messaging campaign.
    """
    __tablename__ = "campaigns"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False, index=True)
    template_name = Column(String(255), nullable=True)
    template_language = Column(String(10), default="en", nullable=False)
    message_body = Column(Text, nullable=True)
    target_segment = Column(String(255), nullable=True)
    status = Column(String(50), default="draft", nullable=False, index=True)  # draft, scheduled, running, completed, paused, failed
    scheduled_at = Column(DateTime, nullable=True, index=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    # Aggregated counters for fast KPI retrieval
    total_recipients = Column(Integer, default=0, nullable=False)
    sent_count = Column(Integer, default=0, nullable=False)
    delivered_count = Column(Integer, default=0, nullable=False)
    read_count = Column(Integer, default=0, nullable=False)
    failed_count = Column(Integer, default=0, nullable=False)

    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    recipients = relationship(
        "CampaignRecipient",
        back_populates="campaign",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    message_logs = relationship(
        "MessageLog",
        back_populates="campaign",
        lazy="selectin",
    )
    link_clicks = relationship(
        "LinkClickRecord",
        back_populates="campaign",
        lazy="selectin",
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "template_name": self.template_name,
            "template_language": self.template_language,
            "message_body": self.message_body,
            "target_segment": self.target_segment,
            "status": self.status,
            "scheduled_at": self.scheduled_at.isoformat() if self.scheduled_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "total_recipients": self.total_recipients,
            "sent_count": self.sent_count,
            "delivered_count": self.delivered_count,
            "read_count": self.read_count,
            "failed_count": self.failed_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:
        return f"<Campaign(id={self.id}, name={self.name!r}, status={self.status!r}, sent={self.sent_count})>"


# ==============================================================================
# 4. CAMPAIGN RECIPIENTS
# ==============================================================================

class CampaignRecipient(Base):
    """
    Junction record between a Campaign and a Contact.
    Enforces uniqueness: a contact cannot be targeted more than once per campaign.
    """
    __tablename__ = "campaign_recipients"

    id = Column(Integer, primary_key=True, autoincrement=True)
    campaign_id = Column(
        Integer,
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    contact_id = Column(
        Integer,
        ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status = Column(String(50), default="pending", nullable=False, index=True)  # pending, queued, sent, delivered, read, failed, skipped
    sent_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Unique constraint ensuring one recipient entry per contact per campaign
    __table_args__ = (
        UniqueConstraint("campaign_id", "contact_id", name="uq_campaign_recipients_campaign_contact"),
    )

    # Relationships
    campaign = relationship("Campaign", back_populates="recipients")
    contact = relationship("Contact", back_populates="campaign_recipients")
    message_logs = relationship(
        "MessageLog",
        back_populates="campaign_recipient",
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "campaign_id": self.campaign_id,
            "contact_id": self.contact_id,
            "status": self.status,
            "sent_at": self.sent_at.isoformat() if self.sent_at else None,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:
        return f"<CampaignRecipient(id={self.id}, campaign_id={self.campaign_id}, contact_id={self.contact_id}, status={self.status!r})>"


# ==============================================================================
# 5. MESSAGE LOGS
# ==============================================================================

class MessageLog(Base):
    """
    Represents an individual outbound WhatsApp message dispatched via Cloud API.
    Tracks WhatsApp Message ID (wamid) and final delivery status.
    """
    __tablename__ = "message_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    whatsapp_message_id = Column(String(255), unique=True, nullable=True, index=True)  # wamid.HBg...
    campaign_id = Column(
        Integer,
        ForeignKey("campaigns.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    contact_id = Column(
        Integer,
        ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    campaign_recipient_id = Column(
        Integer,
        ForeignKey("campaign_recipients.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    phone_number = Column(String(32), nullable=False, index=True)
    message_type = Column(String(50), default="template", nullable=False)  # template, text, media, interactive
    message_content = Column(Text, nullable=True)
    status = Column(String(50), default="queued", nullable=False, index=True)  # queued, sent, delivered, read, failed

    sent_at = Column(DateTime, nullable=True)
    delivered_at = Column(DateTime, nullable=True)
    read_at = Column(DateTime, nullable=True)
    failed_at = Column(DateTime, nullable=True)

    error_code = Column(String(50), nullable=True)
    error_message = Column(Text, nullable=True)

    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    campaign = relationship("Campaign", back_populates="message_logs")
    contact = relationship("Contact", back_populates="message_logs")
    campaign_recipient = relationship("CampaignRecipient", back_populates="message_logs")
    status_events = relationship(
        "MessageStatusEvent",
        back_populates="message_log",
        cascade="all, delete-orphan",
        order_by="desc(MessageStatusEvent.event_timestamp)",
        lazy="selectin",
    )
    link_clicks = relationship(
        "LinkClickRecord",
        back_populates="message_log",
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "whatsapp_message_id": self.whatsapp_message_id,
            "campaign_id": self.campaign_id,
            "contact_id": self.contact_id,
            "campaign_recipient_id": self.campaign_recipient_id,
            "phone_number": self.phone_number,
            "message_type": self.message_type,
            "message_content": self.message_content,
            "status": self.status,
            "sent_at": self.sent_at.isoformat() if self.sent_at else None,
            "delivered_at": self.delivered_at.isoformat() if self.delivered_at else None,
            "read_at": self.read_at.isoformat() if self.read_at else None,
            "failed_at": self.failed_at.isoformat() if self.failed_at else None,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:
        return f"<MessageLog(id={self.id}, wamid={self.whatsapp_message_id!r}, phone={self.phone_number!r}, status={self.status!r})>"


# ==============================================================================
# 6. MESSAGE STATUS EVENTS
# ==============================================================================

class MessageStatusEvent(Base):
    """
    Granular audit log of each status transition received via Meta Webhooks.
    (e.g., sent -> delivered -> read or sent -> failed).
    """
    __tablename__ = "message_status_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    message_log_id = Column(
        Integer,
        ForeignKey("message_logs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status = Column(String(50), nullable=False, index=True)  # sent, delivered, read, failed
    event_timestamp = Column(DateTime, nullable=False, index=True)
    raw_payload = Column(Text, nullable=True)  # Full JSON payload received from webhook
    error_code = Column(String(50), nullable=True)
    error_details = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    # Relationships
    message_log = relationship("MessageLog", back_populates="status_events")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "message_log_id": self.message_log_id,
            "status": self.status,
            "event_timestamp": self.event_timestamp.isoformat() if self.event_timestamp else None,
            "raw_payload": self.raw_payload,
            "error_code": self.error_code,
            "error_details": self.error_details,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:
        return f"<MessageStatusEvent(id={self.id}, message_log_id={self.message_log_id}, status={self.status!r}, ts={self.event_timestamp})>"


# ==============================================================================
# 7. LINK CLICK RECORDS
# ==============================================================================

class LinkClickRecord(Base):
    """
    Tracks click-through events for campaign links redirected through the platform.
    """
    __tablename__ = "link_click_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    campaign_id = Column(
        Integer,
        ForeignKey("campaigns.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    contact_id = Column(
        Integer,
        ForeignKey("contacts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    message_log_id = Column(
        Integer,
        ForeignKey("message_logs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    url_code = Column(String(64), nullable=True, index=True)  # Short tracking code
    original_url = Column(String(2048), nullable=False)
    ip_address = Column(String(64), nullable=True)
    user_agent = Column(String(512), nullable=True)
    clicked_at = Column(DateTime, server_default=func.now(), nullable=False, index=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    # Relationships
    campaign = relationship("Campaign", back_populates="link_clicks")
    contact = relationship("Contact", back_populates="link_clicks")
    message_log = relationship("MessageLog", back_populates="link_clicks")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "campaign_id": self.campaign_id,
            "contact_id": self.contact_id,
            "message_log_id": self.message_log_id,
            "url_code": self.url_code,
            "original_url": self.original_url,
            "ip_address": self.ip_address,
            "user_agent": self.user_agent,
            "clicked_at": self.clicked_at.isoformat() if self.clicked_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:
        return f"<LinkClickRecord(id={self.id}, campaign_id={self.campaign_id}, url_code={self.url_code!r}, clicked_at={self.clicked_at})>"
