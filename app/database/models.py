import enum
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class MessageType(str, enum.Enum):
    TEXT = "TEXT"
    PHOTO = "PHOTO"


class DeliveryStatus(str, enum.Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    RETRY = "RETRY"
    SKIPPED = "SKIPPED"


class SenderAccount(Base):
    __tablename__ = "sender_accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(80), unique=True)
    telegram_user_id: Mapped[int] = mapped_column(unique=True)
    username: Mapped[str | None] = mapped_column(String(80))
    display_name: Mapped[str] = mapped_column(String(200))
    session_name: Mapped[str] = mapped_column(String(120), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    connection_status: Mapped[str] = mapped_column(String(30), default="UNKNOWN")


class TargetChat(Base):
    __tablename__ = "target_chats"
    __table_args__ = (UniqueConstraint("sender_account_id", "telegram_chat_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    sender_account_id: Mapped[int] = mapped_column(ForeignKey("sender_accounts.id", ondelete="RESTRICT"), index=True)
    telegram_chat_id: Mapped[int]
    title: Mapped[str] = mapped_column(String(250))
    username: Mapped[str | None] = mapped_column(String(80))
    chat_type: Mapped[str] = mapped_column(String(30))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Campaign(Base):
    __tablename__ = "campaigns"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(150))
    sender_account_id: Mapped[int] = mapped_column(ForeignKey("sender_accounts.id", ondelete="RESTRICT"), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    interval_seconds: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    rotation_index: Mapped[int] = mapped_column(default=0)
    timezone: Mapped[str] = mapped_column(String(50), default="Asia/Tehran")
    failure_count: Mapped[int] = mapped_column(default=0)
    execution_token: Mapped[str | None] = mapped_column(String(64), index=True)
    execution_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    messages: Mapped[list["CampaignMessage"]] = relationship(cascade="all, delete-orphan", lazy="selectin")
    targets: Mapped[list[TargetChat]] = relationship(secondary="campaign_targets", lazy="selectin")


class CampaignTarget(Base):
    __tablename__ = "campaign_targets"
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), primary_key=True)
    target_chat_id: Mapped[int] = mapped_column(ForeignKey("target_chats.id", ondelete="RESTRICT"), primary_key=True)


class CampaignMessage(Base):
    __tablename__ = "campaign_messages"
    __table_args__ = (UniqueConstraint("campaign_id", "position"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    position: Mapped[int]
    message_type: Mapped[MessageType] = mapped_column(Enum(MessageType))
    text: Mapped[str | None] = mapped_column(Text)
    parse_mode: Mapped[str] = mapped_column(String(20), default="HTML")
    media_path: Mapped[str | None] = mapped_column(String(500))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DeliveryLog(Base):
    __tablename__ = "delivery_logs"
    __table_args__ = (Index("ix_delivery_report", "campaign_id", "attempted_at", "status"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id", ondelete="SET NULL"), index=True)
    campaign_message_id: Mapped[int | None] = mapped_column(ForeignKey("campaign_messages.id", ondelete="SET NULL"))
    sender_account_id: Mapped[int] = mapped_column(ForeignKey("sender_accounts.id", ondelete="RESTRICT"), index=True)
    target_chat_id: Mapped[int] = mapped_column(ForeignKey("target_chats.id", ondelete="RESTRICT"), index=True)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[DeliveryStatus] = mapped_column(Enum(DeliveryStatus), index=True)
    telegram_message_id: Mapped[int | None]
    error_type: Mapped[str | None] = mapped_column(String(120))
    error_message: Mapped[str | None] = mapped_column(Text)
