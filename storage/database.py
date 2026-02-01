"""
Database for metadata storage.

Uses SQLAlchemy for portable database access (PostgreSQL or SQLite).
"""

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
    Index,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.types import TypeDecorator
import json

import structlog

from config.settings import get_settings

logger = structlog.get_logger(__name__)

Base = declarative_base()


class JSONType(TypeDecorator):
    """JSON type that works with both PostgreSQL and SQLite."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None:
            return json.dumps(value)
        return value

    def process_result_value(self, value, dialect):
        if value is not None:
            return json.loads(value)
        return value


class DiscoveredURLModel(Base):
    """Model for discovered URLs."""

    __tablename__ = "discovered_urls"

    id = Column(Integer, primary_key=True, autoincrement=True)
    url = Column(String(2048), nullable=False, unique=True)
    domain = Column(String(255), nullable=False, index=True)
    canonical_url = Column(String(2048), index=True)
    discovered_at = Column(DateTime, default=datetime.utcnow)
    status = Column(String(50), default="pending", index=True)
    priority = Column(Integer, default=0)
    depth = Column(Integer, default=0)
    retry_count = Column(Integer, default=0)
    last_error = Column(Text)
    job_id = Column(String(100), index=True)

    __table_args__ = (
        Index("ix_urls_domain_status", "domain", "status"),
    )


class GemstoneItemModel(Base):
    """Model for scraped gemstone items."""

    __tablename__ = "gemstone_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    item_id = Column(String(100), nullable=False, unique=True, index=True)
    source_site = Column(String(255), nullable=False, index=True)
    source_url = Column(String(2048), nullable=False)
    canonical_url = Column(String(2048), index=True)
    discovered_at = Column(DateTime)
    scraped_at = Column(DateTime, default=datetime.utcnow)
    title = Column(Text)
    description_text = Column(Text)
    raw_html_path = Column(String(512))
    stone_type_claimed = Column(String(100), index=True)
    shape = Column(String(100))
    color_family = Column(String(100))
    carat = Column(Float)
    lab_name = Column(String(100))
    lab_report_id = Column(String(100), index=True)
    has_lab_report = Column(Boolean, default=False, index=True)
    image_urls = Column(JSONType)
    downloaded_images = Column(JSONType)
    extracted_attributes = Column(JSONType)
    validation_tier = Column(Integer, index=True)
    validation_confidence = Column(Float)
    validation_keep = Column(Boolean, default=True, index=True)
    validation_reject_reasons = Column(JSONType)
    is_duplicate = Column(Boolean, default=False, index=True)
    duplicate_of = Column(String(100))
    compliance_robots_allowed = Column(Boolean, default=True)
    compliance_tos_allowed = Column(Boolean, default=True)
    job_id = Column(String(100), index=True)

    __table_args__ = (
        Index("ix_items_tier_keep", "validation_tier", "validation_keep"),
    )


class CrawlJobModel(Base):
    """Model for crawl jobs."""

    __tablename__ = "crawl_jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(100), nullable=False, unique=True, index=True)
    domain = Column(String(255), nullable=False, index=True)
    status = Column(String(50), default="pending", index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    urls_discovered = Column(Integer, default=0)
    urls_fetched = Column(Integer, default=0)
    urls_parsed = Column(Integer, default=0)
    urls_validated = Column(Integer, default=0)
    urls_failed = Column(Integer, default=0)
    items_kept = Column(Integer, default=0)
    items_rejected = Column(Integer, default=0)
    error_message = Column(Text)
    config = Column(JSONType)


class Database:
    """
    Database interface for metadata storage.

    Provides methods for storing and querying pipeline metadata.
    """

    def __init__(self, database_url: Optional[str] = None):
        """
        Initialize the database.

        Args:
            database_url: SQLAlchemy database URL
        """
        settings = get_settings()
        self.database_url = database_url or settings.database_url

        self.engine = create_engine(
            self.database_url,
            pool_pre_ping=True,
            echo=settings.debug,
        )
        self.SessionLocal = sessionmaker(bind=self.engine)

        # Create tables
        Base.metadata.create_all(self.engine)
        logger.info("database_initialized", url=self.database_url.split("@")[-1])

    def get_session(self) -> Session:
        """Get a new database session."""
        return self.SessionLocal()

    # URL operations

    def add_url(self, url_data: dict) -> bool:
        """Add a discovered URL."""
        session = self.get_session()
        try:
            existing = session.query(DiscoveredURLModel).filter_by(
                url=url_data["url"]
            ).first()
            if existing:
                return False

            url_model = DiscoveredURLModel(**url_data)
            session.add(url_model)
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error("add_url_error", error=str(e))
            return False
        finally:
            session.close()

    def get_pending_urls(
        self,
        domain: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        """Get pending URLs for processing."""
        session = self.get_session()
        try:
            query = session.query(DiscoveredURLModel).filter_by(status="pending")
            if domain:
                query = query.filter_by(domain=domain)
            query = query.order_by(
                DiscoveredURLModel.priority.desc(),
                DiscoveredURLModel.discovered_at.asc(),
            ).limit(limit)

            results = []
            for url in query.all():
                results.append({
                    "url": url.url,
                    "domain": url.domain,
                    "priority": url.priority,
                    "depth": url.depth,
                })
            return results
        finally:
            session.close()

    def update_url_status(
        self,
        url: str,
        status: str,
        error: Optional[str] = None,
    ) -> bool:
        """Update URL status."""
        session = self.get_session()
        try:
            url_model = session.query(DiscoveredURLModel).filter_by(url=url).first()
            if url_model:
                url_model.status = status
                if error:
                    url_model.last_error = error
                    url_model.retry_count += 1
                session.commit()
                return True
            return False
        except Exception as e:
            session.rollback()
            logger.error("update_url_error", error=str(e))
            return False
        finally:
            session.close()

    # Item operations

    def add_item(self, item_data: dict) -> bool:
        """Add a scraped item."""
        session = self.get_session()
        try:
            item_model = GemstoneItemModel(**item_data)
            session.add(item_model)
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error("add_item_error", error=str(e))
            return False
        finally:
            session.close()

    def get_item(self, item_id: str) -> Optional[dict]:
        """Get an item by ID."""
        session = self.get_session()
        try:
            item = session.query(GemstoneItemModel).filter_by(item_id=item_id).first()
            if item:
                return {
                    "item_id": item.item_id,
                    "source_site": item.source_site,
                    "source_url": item.source_url,
                    "title": item.title,
                    "stone_type_claimed": item.stone_type_claimed,
                    "validation_tier": item.validation_tier,
                    "validation_keep": item.validation_keep,
                }
            return None
        finally:
            session.close()

    def get_validated_items(
        self,
        tier: Optional[int] = None,
        keep_only: bool = True,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[dict]:
        """Get validated items."""
        session = self.get_session()
        try:
            query = session.query(GemstoneItemModel)
            if keep_only:
                query = query.filter_by(validation_keep=True)
            if tier is not None:
                query = query.filter_by(validation_tier=tier)

            query = query.order_by(GemstoneItemModel.scraped_at.desc())
            if offset:
                query = query.offset(offset)
            if limit:
                query = query.limit(limit)

            results = []
            for item in query.all():
                results.append({
                    "item_id": item.item_id,
                    "source_site": item.source_site,
                    "source_url": item.source_url,
                    "title": item.title,
                    "description_text": item.description_text,
                    "stone_type_claimed": item.stone_type_claimed,
                    "shape": item.shape,
                    "color_family": item.color_family,
                    "carat": item.carat,
                    "lab_name": item.lab_name,
                    "lab_report_id": item.lab_report_id,
                    "has_lab_report": item.has_lab_report,
                    "image_urls": item.image_urls,
                    "downloaded_images": item.downloaded_images,
                    "validation_tier": item.validation_tier,
                    "validation_confidence": item.validation_confidence,
                    "validation_keep": item.validation_keep,
                    "validation_reject_reasons": item.validation_reject_reasons,
                    "is_duplicate": item.is_duplicate,
                    "duplicate_of": item.duplicate_of,
                })
            return results
        finally:
            session.close()

    # Job operations

    def create_job(self, job_data: dict) -> bool:
        """Create a crawl job."""
        session = self.get_session()
        try:
            job_model = CrawlJobModel(**job_data)
            session.add(job_model)
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error("create_job_error", error=str(e))
            return False
        finally:
            session.close()

    def update_job(self, job_id: str, updates: dict) -> bool:
        """Update a job."""
        session = self.get_session()
        try:
            job = session.query(CrawlJobModel).filter_by(job_id=job_id).first()
            if job:
                for key, value in updates.items():
                    setattr(job, key, value)
                session.commit()
                return True
            return False
        except Exception as e:
            session.rollback()
            logger.error("update_job_error", error=str(e))
            return False
        finally:
            session.close()

    def get_job(self, job_id: str) -> Optional[dict]:
        """Get a job by ID."""
        session = self.get_session()
        try:
            job = session.query(CrawlJobModel).filter_by(job_id=job_id).first()
            if job:
                return {
                    "job_id": job.job_id,
                    "domain": job.domain,
                    "status": job.status,
                    "urls_discovered": job.urls_discovered,
                    "urls_fetched": job.urls_fetched,
                    "items_kept": job.items_kept,
                    "items_rejected": job.items_rejected,
                }
            return None
        finally:
            session.close()

    # Stats

    def get_stats(self) -> dict:
        """Get overall statistics."""
        session = self.get_session()
        try:
            total_urls = session.query(DiscoveredURLModel).count()
            pending_urls = session.query(DiscoveredURLModel).filter_by(status="pending").count()
            total_items = session.query(GemstoneItemModel).count()
            kept_items = session.query(GemstoneItemModel).filter_by(validation_keep=True).count()

            # Items by tier
            tier_counts = {}
            for tier in range(4):
                count = session.query(GemstoneItemModel).filter_by(
                    validation_tier=tier, validation_keep=True
                ).count()
                tier_counts[tier] = count

            return {
                "total_urls": total_urls,
                "pending_urls": pending_urls,
                "total_items": total_items,
                "kept_items": kept_items,
                "items_by_tier": tier_counts,
            }
        finally:
            session.close()


# Global instance
_database: Optional[Database] = None


def get_database() -> Database:
    """Get the global database instance."""
    global _database
    if _database is None:
        _database = Database()
    return _database
