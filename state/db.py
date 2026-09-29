"""SQLite persistence so the restorer can stop and resume without data loss."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Boolean,
    Float,
    Integer,
    MetaData,
    String,
    Text,
    create_engine,
    select,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

UTC = timezone.utc


class Base(DeclarativeBase):
    metadata = MetaData()


class UrlRow(Base):
    __tablename__ = "urls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    original_url: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    url_hash: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    urlkey: Mapped[str | None] = mapped_column(String, nullable=True)
    discovered_at: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, default="discovered")


class SnapshotRow(Base):
    __tablename__ = "snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url_id: Mapped[int] = mapped_column(Integer, nullable=False)
    timestamp: Mapped[str] = mapped_column(String, nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String, nullable=True)
    digest: Mapped[str | None] = mapped_column(String, nullable=True)
    length: Mapped[int] = mapped_column(Integer, default=0)
    archive_url: Mapped[str] = mapped_column(String, nullable=False)
    score: Mapped[float] = mapped_column(Float, default=0)
    selected: Mapped[bool] = mapped_column(Boolean, default=False)


class PageRow(Base):
    __tablename__ = "pages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url_id: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    html_path: Mapped[str | None] = mapped_column(String, nullable=True)
    parsed_json_path: Mapped[str | None] = mapped_column(String, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_type: Mapped[str | None] = mapped_column(String, nullable=True)
    downloaded_at: Mapped[str | None] = mapped_column(String, nullable=True)
    parse_status: Mapped[str] = mapped_column(String, default="pending")


class MediaRow(Base):
    __tablename__ = "media"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    original_url: Mapped[str] = mapped_column(String, nullable=False)
    archive_url: Mapped[str] = mapped_column(String, nullable=False)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    local_path: Mapped[str | None] = mapped_column(String, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String, nullable=True)
    source_url: Mapped[str] = mapped_column(String, nullable=False)
    downloaded: Mapped[bool] = mapped_column(Boolean, default=False)
    skipped_reason: Mapped[str | None] = mapped_column(String, nullable=True)


class ImportRow(Base):
    __tablename__ = "imports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    source_url: Mapped[str] = mapped_column(String, nullable=False)
    snapshot_url: Mapped[str | None] = mapped_column(String, nullable=True)
    wp_post_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wp_url: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="pending")
    imported_at: Mapped[str | None] = mapped_column(String, nullable=True)
    match_score: Mapped[float | None] = mapped_column(Float, nullable=True)


class ErrorRow(Base):
    __tablename__ = "errors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url: Mapped[str] = mapped_column(String, nullable=False)
    stage: Mapped[str] = mapped_column(String, nullable=False)
    error: Mapped[str] = mapped_column(Text, nullable=False)
    exception: Mapped[str | None] = mapped_column(Text, nullable=True)
    timestamp: Mapped[str] = mapped_column(String, nullable=False)


class UrlMappingRow(Base):
    __tablename__ = "url_mapping"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    old_url: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    new_url: Mapped[str | None] = mapped_column(String, nullable=True)
    post_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String, default="pending")


class StateStore:
    def __init__(self, sqlite_path: Path) -> None:
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(f"sqlite:///{sqlite_path}", future=True)
        Base.metadata.create_all(self.engine)
        self._session = sessionmaker(bind=self.engine, expire_on_commit=False)

    def session(self) -> Session:
        return self._session()

    def upsert_url(self, original_url: str, url_hash: str, urlkey: str | None = None) -> UrlRow:
        with self.session() as session:
            row = session.scalar(select(UrlRow).where(UrlRow.original_url == original_url))
            if row is None:
                row = UrlRow(
                    original_url=original_url,
                    url_hash=url_hash,
                    urlkey=urlkey,
                    discovered_at=datetime.now(UTC).isoformat(),
                    status="discovered",
                )
                session.add(row)
                session.commit()
                session.refresh(row)
            return row

    def upsert_snapshot(self, url_id: int, payload: dict[str, Any], selected: bool = False) -> None:
        with self.session() as session:
            row = session.scalar(
                select(SnapshotRow).where(
                    SnapshotRow.url_id == url_id,
                    SnapshotRow.timestamp == payload["timestamp"],
                )
            )
            if row is None:
                row = SnapshotRow(url_id=url_id, timestamp=payload["timestamp"], archive_url="")
                session.add(row)
            row.status_code = payload.get("status_code")
            row.mime_type = payload.get("mime_type")
            row.digest = payload.get("digest")
            row.length = int(payload.get("length") or 0)
            row.archive_url = payload["archive_url"]
            row.score = float(payload.get("score") or 0)
            row.selected = selected
            session.commit()

    def mark_selected(self, url_id: int, timestamp: str) -> None:
        with self.session() as session:
            rows = session.scalars(select(SnapshotRow).where(SnapshotRow.url_id == url_id)).all()
            for row in rows:
                row.selected = row.timestamp == timestamp
            session.commit()

    def record_page(self, url_id: int, **kwargs: Any) -> None:
        with self.session() as session:
            row = session.scalar(select(PageRow).where(PageRow.url_id == url_id))
            if row is None:
                row = PageRow(url_id=url_id)
                session.add(row)
            for key, value in kwargs.items():
                setattr(row, key, value)
            session.commit()

    def record_media(self, asset: dict[str, Any]) -> None:
        with self.session() as session:
            row = session.scalar(
                select(MediaRow).where(
                    MediaRow.original_url == asset["original_url"],
                    MediaRow.source_url == asset["source_page"],
                )
            )
            if row is None:
                row = MediaRow(
                    original_url=asset["original_url"],
                    archive_url=asset["archive_url"],
                    filename=asset["filename"],
                    source_url=asset["source_page"],
                )
                session.add(row)
            row.local_path = asset.get("local_path")
            row.mime_type = asset.get("mime_type")
            row.sha256 = asset.get("sha256")
            row.downloaded = bool(asset.get("downloaded"))
            row.skipped_reason = asset.get("skipped_reason")
            session.commit()

    def record_import(self, url_id: int, payload: dict[str, Any]) -> None:
        with self.session() as session:
            row = session.scalar(select(ImportRow).where(ImportRow.url_id == url_id))
            if row is None:
                row = ImportRow(url_id=url_id, source_url=payload["source_url"])
                session.add(row)
            for key, value in payload.items():
                setattr(row, key, value)
            session.commit()

    def map_url(self, old_url: str, new_url: str | None, post_id: int | None, status: str) -> None:
        with self.session() as session:
            row = session.scalar(select(UrlMappingRow).where(UrlMappingRow.old_url == old_url))
            if row is None:
                row = UrlMappingRow(old_url=old_url)
                session.add(row)
            row.new_url = new_url
            row.post_id = post_id
            row.status = status
            session.commit()

    def record_error(self, url: str, stage: str, error: str, exception: str | None = None) -> None:
        with self.session() as session:
            session.add(
                ErrorRow(
                    url=url,
                    stage=stage,
                    error=error,
                    exception=exception,
                    timestamp=datetime.now(UTC).isoformat(),
                )
            )
            session.commit()

    def failed_urls(self) -> list[ErrorRow]:
        with self.session() as session:
            return list(session.scalars(select(ErrorRow).order_by(ErrorRow.id.desc())).all())

    def counts(self) -> dict[str, int]:
        with self.session() as session:
            return {
                "urls": session.scalar(text("SELECT COUNT(*) FROM urls")) or 0,
                "snapshots": session.scalar(text("SELECT COUNT(*) FROM snapshots")) or 0,
                "pages": session.scalar(text("SELECT COUNT(*) FROM pages")) or 0,
                "media": session.scalar(text("SELECT COUNT(*) FROM media")) or 0,
                "imports": session.scalar(text("SELECT COUNT(*) FROM imports")) or 0,
                "errors": session.scalar(text("SELECT COUNT(*) FROM errors")) or 0,
            }

    def imported_source_urls(self) -> set[str]:
        with self.session() as session:
            rows = session.scalars(select(ImportRow).where(ImportRow.status == "imported")).all()
            return {row.source_url for row in rows}

    def append_jsonl(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")
