from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, Numeric,
    String, Text, UniqueConstraint, text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class City(Base):
    __tablename__ = "cities"
    __table_args__ = (UniqueConstraint("name", "region"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    region: Mapped[str] = mapped_column(String(150), default="", server_default="")
    external_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    slug: Mapped[str | None] = mapped_column(String(200))
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 7))
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 7))
    population: Mapped[int | None] = mapped_column(Integer)
    house_radius_km: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))


class Address(Base):
    __tablename__ = "addresses"
    __table_args__ = (UniqueConstraint("city_id", "text"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    city_id: Mapped[int] = mapped_column(ForeignKey("cities.id"))
    text: Mapped[str] = mapped_column(Text)
    map_key: Mapped[str | None] = mapped_column(String(500), index=True)
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 7))
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 7))
    coordinate_source: Mapped[str | None] = mapped_column(String(20))
    geolocated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MapBuilding(Base):
    __tablename__ = "map_buildings"
    city_id: Mapped[int] = mapped_column(ForeignKey("cities.id"), primary_key=True)
    key: Mapped[str] = mapped_column(String(500), primary_key=True)
    latitude: Mapped[Decimal] = mapped_column(Numeric(10, 7))
    longitude: Mapped[Decimal] = mapped_column(Numeric(10, 7))


class MapImport(Base):
    __tablename__ = "map_imports"
    city_id: Mapped[int] = mapped_column(ForeignKey("cities.id"), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    building_count: Mapped[int] = mapped_column(Integer, default=0)


class Complex(Base):
    __tablename__ = "residential_complexes"
    __table_args__ = (UniqueConstraint("city_id", "name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    city_id: Mapped[int] = mapped_column(ForeignKey("cities.id"))
    name: Mapped[str] = mapped_column(String(500))


class PropertyKind(Base):
    __tablename__ = 'property_kinds'
    code: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(50), unique=True)


class MarketType(Base):
    __tablename__ = "market_types"
    code: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(50), unique=True)


class FeatureType(Base):
    __tablename__ = "feature_types"
    code: Mapped[str] = mapped_column(String(30), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)


class FeatureValue(Base):
    __tablename__ = "feature_values"
    __table_args__ = (UniqueConstraint("type_code", "value"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    type_code: Mapped[str] = mapped_column(ForeignKey("feature_types.code"))
    value: Mapped[str] = mapped_column(String(1000))
    feature_type: Mapped[FeatureType] = relationship()


class ListingFeature(Base):
    __tablename__ = "listing_features"
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"), primary_key=True)
    value_id: Mapped[int] = mapped_column(ForeignKey("feature_values.id"), primary_key=True)


class ListingCityScope(Base):
    __tablename__ = 'listing_city_scopes'
    listing_id: Mapped[int] = mapped_column(ForeignKey('listings.id', ondelete='CASCADE'), primary_key=True)
    city_id: Mapped[int] = mapped_column(ForeignKey('cities.id'), primary_key=True)
    distance_km: Mapped[Decimal | None] = mapped_column(Numeric(9, 2))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ListingStateScope(Base):
    __tablename__ = 'listing_state_scopes'
    state_id: Mapped[int] = mapped_column(ForeignKey('listing_states.id', ondelete='CASCADE'), primary_key=True)
    city_id: Mapped[int] = mapped_column(ForeignKey('cities.id'), primary_key=True)
    distance_km: Mapped[Decimal | None] = mapped_column(Numeric(9, 2))


class Listing(Base):
    __tablename__ = "listings"
    __table_args__ = (
        CheckConstraint("rooms >= 0"),
        CheckConstraint("rooms IS NOT NULL OR is_open_plan OR property_kind = 'house'", name="ck_listings_rooms_known_or_open_plan"),
        CheckConstraint("total_area > 0"),
        CheckConstraint("kitchen_area IS NULL OR kitchen_area > 0"),
        CheckConstraint("living_area IS NULL OR living_area > 0"),
        CheckConstraint("ceiling_height IS NULL OR ceiling_height > 0"),
        CheckConstraint("construction_year IS NULL OR construction_year BETWEEN 1000 AND 2999", name="ck_listings_construction_year"),
        CheckConstraint("floors_total IS NULL OR floors_total > 0"),
        CheckConstraint("floor IS NULL OR floors_total IS NULL OR floor <= floors_total"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    avito_id: Mapped[str] = mapped_column(String(30), unique=True)
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    property_kind: Mapped[str] = mapped_column(ForeignKey('property_kinds.code'), default='apartment', server_default='apartment')
    land_area: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    distance_to_center_km: Mapped[Decimal | None] = mapped_column(Numeric(9, 2))
    source_distance_km: Mapped[Decimal | None] = mapped_column(Numeric(9, 2))
    market_type_code: Mapped[str] = mapped_column(ForeignKey("market_types.code"))
    address_id: Mapped[int] = mapped_column(ForeignKey("addresses.id"))
    complex_id: Mapped[int | None] = mapped_column(ForeignKey("residential_complexes.id"))
    rooms: Mapped[int | None] = mapped_column(Integer)
    is_open_plan: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    total_area: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    kitchen_area: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    living_area: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    floor: Mapped[int | None] = mapped_column(Integer)
    floors_total: Mapped[int | None] = mapped_column(Integer)
    ceiling_height: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    construction_year: Mapped[int | None] = mapped_column(Integer)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), index=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archive_reason: Mapped[str | None] = mapped_column(Text)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    scopes: Mapped[list[ListingCityScope]] = relationship(lazy='selectin', cascade='all, delete-orphan')
    address: Mapped[Address] = relationship(lazy="selectin")
    complex: Mapped[Complex | None] = relationship(lazy="selectin")
    market_type: Mapped[MarketType] = relationship(lazy="selectin")
    features: Mapped[list[FeatureValue]] = relationship(secondary="listing_features", lazy="selectin")


class ListingStateFeature(Base):
    __tablename__ = "listing_state_features"
    state_id: Mapped[int] = mapped_column(ForeignKey("listing_states.id", ondelete="CASCADE"), primary_key=True)
    value_id: Mapped[int] = mapped_column(ForeignKey("feature_values.id"), primary_key=True)


class ListingState(Base):
    """Observed characteristics and status; prices keep their own change history."""
    __tablename__ = "listing_states"
    scopes: Mapped[list[ListingStateScope]] = relationship(lazy="selectin", cascade="all, delete-orphan")
    __table_args__ = (Index("ix_listing_states_time", "listing_id", "observed_at", "id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    property_kind: Mapped[str] = mapped_column(ForeignKey('property_kinds.code'), default='apartment', server_default='apartment')
    land_area: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    distance_to_center_km: Mapped[Decimal | None] = mapped_column(Numeric(9, 2))
    source_distance_km: Mapped[Decimal | None] = mapped_column(Numeric(9, 2))
    market_type_code: Mapped[str] = mapped_column(ForeignKey("market_types.code"))
    address_id: Mapped[int] = mapped_column(ForeignKey("addresses.id"))
    complex_id: Mapped[int | None] = mapped_column(ForeignKey("residential_complexes.id"))
    rooms: Mapped[int | None] = mapped_column(Integer)
    is_open_plan: Mapped[bool] = mapped_column(Boolean)
    total_area: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    kitchen_area: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    living_area: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    floor: Mapped[int | None] = mapped_column(Integer)
    floors_total: Mapped[int | None] = mapped_column(Integer)
    ceiling_height: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    construction_year: Mapped[int | None] = mapped_column(Integer)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_archived: Mapped[bool] = mapped_column(Boolean)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archive_reason: Mapped[str | None] = mapped_column(Text)
    features: Mapped[list[FeatureValue]] = relationship(secondary="listing_state_features", lazy="selectin")


class DuplicateReview(Base):
    __tablename__ = 'duplicate_reviews'
    __table_args__ = (
        CheckConstraint('left_id < right_id', name='ck_duplicate_review_order'),
        CheckConstraint("decision IN ('same','different','pending')", name='ck_duplicate_review_decision'),
        Index('ix_duplicate_review_pair_time', 'left_id', 'right_id', 'reviewed_at', 'id'),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    left_id: Mapped[int] = mapped_column(ForeignKey('listings.id', ondelete='CASCADE'))
    right_id: Mapped[int] = mapped_column(ForeignKey('listings.id', ondelete='CASCADE'))
    decision: Mapped[str] = mapped_column(String(20))
    left_signature: Mapped[str] = mapped_column(String(64))
    right_signature: Mapped[str] = mapped_column(String(64))
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Crawl(Base):
    __tablename__ = "crawls"
    __table_args__ = (
        CheckConstraint("status IN ('active','completed','abandoned')"),
        Index("uq_active_city_kind_mode_crawl", "city_id", "property_kind", "mode", unique=True, postgresql_where=text("status = 'active'")),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    city_id: Mapped[int | None] = mapped_column(ForeignKey("cities.id"))
    mode: Mapped[str] = mapped_column(String(20), default="full", server_default="full")
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    property_kind: Mapped[str] = mapped_column(ForeignKey('property_kinds.code'), default='apartment', server_default='apartment')
    radius_km: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    catalog_url: Mapped[str] = mapped_column(Text)
    skip_existing_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CrawlRange(Base):
    __tablename__ = "crawl_ranges"
    __table_args__ = (
        CheckConstraint("status IN ('pending','active','completed','split','limited')"),
        CheckConstraint("minimum >= 0 AND (maximum IS NULL OR maximum >= minimum)"),
        CheckConstraint("next_page > 0"),
        ForeignKeyConstraint(["crawl_id", "parent_number"], ["crawl_ranges.crawl_id", "crawl_ranges.number"]),
        Index("uq_active_crawl_range", "crawl_id", unique=True, postgresql_where=text("status = 'active'")),
    )
    crawl_id: Mapped[int] = mapped_column(ForeignKey("crawls.id"), primary_key=True)
    number: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_number: Mapped[int | None] = mapped_column(Integer)
    minimum: Mapped[int] = mapped_column(BigInteger, default=1)
    maximum: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(20), default="active")
    next_page: Mapped[int] = mapped_column(Integer, default=1)


class SamplePlan(Base):
    __tablename__ = "sample_plans"
    __table_args__ = (CheckConstraint("probe_retries >= 0", name="ck_sample_probe_retries"),)
    crawl_id: Mapped[int] = mapped_column(ForeignKey("crawls.id"), primary_key=True)
    total_count: Mapped[int | None] = mapped_column(Integer)
    upper_price: Mapped[int] = mapped_column(BigInteger, default=20_000_000)
    phase: Mapped[str] = mapped_column(String(20), default="count")
    probe_retries: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class SampleBoundary(Base):
    __tablename__ = "sample_boundaries"
    crawl_id: Mapped[int] = mapped_column(ForeignKey("sample_plans.crawl_id"), primary_key=True)
    number: Mapped[int] = mapped_column(Integer, primary_key=True)
    low: Mapped[int] = mapped_column(BigInteger)
    high: Mapped[int] = mapped_column(BigInteger)
    price: Mapped[int | None] = mapped_column(BigInteger)


class SampleProbe(Base):
    __tablename__ = "sample_probes"
    crawl_id: Mapped[int] = mapped_column(ForeignKey("sample_plans.crawl_id"), primary_key=True)
    maximum: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    count: Mapped[int] = mapped_column(Integer)


class CrawlPage(Base):
    __tablename__ = "crawl_pages"
    __table_args__ = (ForeignKeyConstraint(["crawl_id", "range_number"], ["crawl_ranges.crawl_id", "crawl_ranges.number"]),)
    crawl_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    range_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    number: Mapped[int] = mapped_column(Integer, primary_key=True)
    url: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64))
    has_next: Mapped[bool | None] = mapped_column(Boolean)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CollectionRun(Base):
    __tablename__ = "collection_runs"
    __table_args__ = (
        CheckConstraint("status IN ('queued','running','completed','partial','blocked','failed','interrupted')"),
        Index("uq_one_active_run", text("(1)"), unique=True,
              postgresql_where=text("status IN ('queued','running')")),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    crawl_id: Mapped[int | None] = mapped_column(ForeignKey("crawls.id"))
    status: Mapped[str] = mapped_column(String(20), default="queued")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    browser_token: Mapped[str | None] = mapped_column(String(32))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pages: Mapped[int] = mapped_column(Integer, default=0)
    found: Mapped[int] = mapped_column(Integer, default=0)
    saved: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    price_changes: Mapped[int] = mapped_column(Integer, default=0)
    archived: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    queue_revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    message: Mapped[str] = mapped_column(Text, default="Ожидание запуска")


class CrawlItem(Base):
    __tablename__ = "crawl_items"
    __table_args__ = (
        CheckConstraint("status IN ('pending','saved','skipped','error','not_selected')"),
        ForeignKeyConstraint(["crawl_id", "range_number", "page_number"],
                             ["crawl_pages.crawl_id", "crawl_pages.range_number", "crawl_pages.number"]),
        Index("ix_crawl_queue", "crawl_id", "status", "page_number", "position"),
        Index("ix_crawl_pending_order", "crawl_id", "range_number", "page_number", "position",
              postgresql_where=text("status = 'pending'")),
    )
    crawl_id: Mapped[int] = mapped_column(ForeignKey("crawls.id"), primary_key=True)
    avito_id: Mapped[str] = mapped_column(String(30), primary_key=True)
    url: Mapped[str] = mapped_column(Text)
    range_number: Mapped[int] = mapped_column(Integer)
    page_number: Mapped[int] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    error: Mapped[str | None] = mapped_column(Text)
    last_run_id: Mapped[int | None] = mapped_column(ForeignKey("collection_runs.id"))
    read_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PriceObservation(Base):
    __tablename__ = "price_observations"
    __table_args__ = (
        CheckConstraint("amount > 0"),
        Index("ix_prices_listing_time", "listing_id", "observed_at", "id"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"))
    run_id: Mapped[int | None] = mapped_column(ForeignKey("collection_runs.id"))
    amount: Mapped[int] = mapped_column(BigInteger)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


FEATURE_TYPES = {
    "balcony": "Балкон или лоджия", "extras": "Дополнительно", "room_type": "Тип комнат",
    "bathroom": "Санузел", "windows": "Окна", "finish": "Отделка",
    "sale_method": "Способ продажи", "sale_conditions": "Условия продажи",
}
