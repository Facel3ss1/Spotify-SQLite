# https://www.python.org/dev/peps/pep-0585/
# and https://www.python.org/dev/peps/pep-0563/
from __future__ import annotations

import datetime
import enum
import re
import sqlite3

import sqlalchemy
from dateutil.parser import isoparse
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.ext.associationproxy import association_proxy
from sqlalchemy.ext.declarative import (
    as_declarative,
    declared_attr,
    has_inherited_table,
)
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.ext.orderinglist import ordering_list
from sqlalchemy.orm import Session, object_session, relationship, validates


@as_declarative()
class Base:
    """
    This is the Base class for the declaritive ORM in SQLAlchemy.

    The class name is converted from CamelCase to snake_case to derive `__tablename__`
    and `__repr__()` is overridden to show the columns of the mapper.
    """

    # @declared_attr makes it a class property, so the first argument is the class, not self
    @declared_attr
    def __tablename__(cls):
        # https://stackoverflow.com/questions/1175208/elegant-python-function-to-convert-camelcase-to-snake-case
        name = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", cls.__name__)
        return re.sub("([a-z0-9])([A-Z])", r"\1_\2", name).lower()

    # This converts a database object into a debugging friendly string
    def __repr__(self):
        columns = self.__mapper__.columns

        if len(columns) > 0:
            # https://docs.python.org/3/library/string.html#format-string-syntax
            columns_repr = ", ".join(
                "{name}={value!r}".format(
                    name=column.name, value=getattr(self, column.name)
                )
                for column in columns
            )

            return f"<{self.__class__.__name__}({columns_repr})>"
        else:
            return super().__repr__()


# Based on https://developer.spotify.com/documentation/web-api/reference/#objects-index

# https://docs.sqlalchemy.org/en/13/orm/extensions/declarative/mixins.html
class SpotifyResource:
    """
    Define a class that can be identified by a Spotify ID and defines hybrid properties
    for the URI and the URLs.

    Inheriting from a `SpotifyResource` creates the saved version of the resource.
    """

    # https://docs.sqlalchemy.org/en/13/orm/extensions/declarative/mixins.html#mixin-inheritance-columns
    @declared_attr.cascading
    def id(cls):
        """
        The ID refers to the Spotify ID (https://developer.spotify.com/documentation/web-api/#spotify-uris-and-ids) of that resource.

        They are in base62 (0-9a-zA-Z) and 22 chars long (https://stackoverflow.com/questions/37980664/spotify-track-id-features).
        """
        if has_inherited_table(cls):
            return Column(
                ForeignKey(f"{cls.resource_name()}.id"),
                primary_key=True,
            )
        else:
            return Column(
                String(22),
                # https://www.sqlite.org/lang_expr.html#regexp
                CheckConstraint("id REGEXP '^[a-zA-z0-9]{22}$'"),
                primary_key=True,
            )

    name: str = Column(Text, nullable=False)
    is_saved: bool = Column(Boolean, nullable=False)
    popularity: int = Column(
        Integer, CheckConstraint("popularity BETWEEN 0 AND 100"), nullable=False
    )

    @classmethod
    def resource_name(cls) -> str:
        """
        The `__tablename__` of the resource at the top of the inheritance hierarchy.

        e.g. This will return `"track"` for both `Track` and `SavedTrack`
        """
        if has_inherited_table(cls):
            # https://stackoverflow.com/questions/2611892/how-to-get-the-parents-of-a-python-class
            return cls.__bases__[0].resource_name()
        else:
            return cls.__tablename__

    # https://docs.sqlalchemy.org/en/13/orm/extensions/hybrid.html
    @hybrid_property
    def external_url(self):
        # We use + so SQLAlchemy can also use this as an SQL expression
        return f"https://open.spotify.com/{self.resource_name()}/" + self.id

    @hybrid_property
    def href(self):
        return f"https://api.spotify.com/v1/{self.resource_name()}s/" + self.id

    @hybrid_property
    def uri(self):
        return f"spotify:{self.resource_name()}:" + self.id

    # When we inherit we set polymorphic_identity to True so is_saved will be True
    # https://docs.sqlalchemy.org/en/13/orm/inheritance.html
    # This will be called for every subclass in the hierarchy because __mapper_args__ is an exception
    # https://docs.sqlalchemy.org/en/13/orm/extensions/declarative/mixins.html#mixin-inheritance-columns
    @declared_attr
    def __mapper_args__(cls):
        if has_inherited_table(cls):
            return {"polymorphic_identity": True}
        else:
            return {"polymorphic_identity": False, "polymorphic_on": cls.is_saved}


album_artist = Table(
    "album_artist",
    Base.metadata,
    Column(
        "album_id",
        ForeignKey("album.id"),
        primary_key=True,
    ),
    Column(
        "artist_id",
        ForeignKey("artist.id"),
        primary_key=True,
    ),
)


# https://docs.sqlalchemy.org/en/13/orm/basic_relationships.html#association-pattern
# We have to use a class instead of a secondary table because artist_order is an additional column
class TrackArtist(Base):
    track_id: str = Column(
        ForeignKey("track.id"),
        primary_key=True,
    )
    artist_id: str = Column(
        ForeignKey("artist.id"),
        primary_key=True,
    )
    # We use an ordering_list to put the artists on a track in the correct order
    artist_order: int = Column(Integer, nullable=False)

    track: Track = relationship("Track", back_populates="track_artists")
    artist: Artist = relationship(
        "Artist",
        back_populates="track_artists",  # cascade="all, delete-orphan"
    )

    @classmethod
    def from_artist(cls, artist: Artist):
        # The track will be added in due to its relationship with TrackArtist
        track_artist = cls()
        track_artist.artist = artist
        return track_artist


class Genre(Base):
    id: int = Column(Integer, primary_key=True)
    name: str = Column(Text(collation="nocase"), nullable=False, unique=True)

    # https://github.com/sqlalchemy/sqlalchemy/wiki/UniqueObject
    @classmethod
    def as_unique(cls, session: Session, name: str) -> Genre:
        """
        Given a `Session` and the name of a genre, return the correct Genre object
        from the database/session.
        """

        cache = getattr(session, "_unique_cache", None)
        if cache is None:
            session._unique_cache = cache = dict()

        if name in cache:
            return cache[name]
        else:
            with session.no_autoflush:
                # Try to fetch the genre from the DB
                unique_genre: Genre = (
                    session.query(cls).filter(cls.name == name).one_or_none()
                )

                if unique_genre is None:
                    # If it isn't in the DB, we return a new Genre to be cached
                    unique_genre = cls(name)
                    session.add(unique_genre)

                cache[name] = unique_genre
                return unique_genre

    def __init__(self, name: str):
        self.name = name


# https://github.com/sqlalchemy/sqlalchemy/wiki/UniqueObjectValidatedOnPending
# See also https://docs.sqlalchemy.org/en/13/orm/extensions/declarative/mixins.html#mixing-in-association-proxy-and-other-attributes
class HasGenres:
    """
    Define a class that can have many genres.

    This defines a many-to-many relationship to the Genre class including an association
    proxy. The genres are checked to make sure they are unique within a session.
    """

    @declared_attr
    def _genres(cls):
        tablename = cls.__tablename__

        # Only one table per subclass will be created because this function is a class property
        secondary_table = Table(
            f"{tablename}_genre",
            Base.metadata,
            Column(
                f"{tablename}_id",
                ForeignKey(f"{tablename}.id"),
                primary_key=True,
            ),
            Column(
                "genre_id",
                ForeignKey("genre.id"),
                primary_key=True,
            ),
        )

        return relationship(
            Genre,
            order_by=Genre.name,
            secondary=secondary_table,
            cascade="all",
            backref=f"{tablename}s",
        )

    # https://docs.sqlalchemy.org/en/13/orm/extensions/associationproxy.html#simplifying-scalar-collections
    @declared_attr
    def genres(cls):
        return association_proxy("_genres", "name")

    @validates("_genres")
    def _validate_genre(self, _, genre: Genre):
        """
        Receive the event that occurs when `someobject._genres` is appended to.

        If the object is present in a Session, then make sure it's the Genre
        object that we looked up from the database.

        Otherwise, do nothing and we'll fix it in `_validate_genre` when the object is
        added to a Session.
        """

        sess = object_session(self)
        if sess is not None:
            return Genre.as_unique(sess, genre.name)
        else:
            return genre


class Artist(SpotifyResource, HasGenres, Base):
    followers: int = Column(Integer, CheckConstraint("followers >= 0"), nullable=False)

    albums: list[Album] = relationship(
        "Album",
        order_by="desc(Album.release_date)",
        secondary=album_artist,
        back_populates="artists",
    )
    track_artists: list[TrackArtist] = relationship(
        "TrackArtist", back_populates="artist"
    )

    tracks = association_proxy("track_artists", "track")

    @classmethod
    def from_json(cls, artist_json) -> Artist:
        artist = cls(
            id=artist_json["id"],
            name=artist_json["name"],
            popularity=artist_json["popularity"],
            followers=artist_json["followers"]["total"],
        )

        artist.genres = artist_json["genres"]

        return artist


class Album(SpotifyResource, HasGenres, Base):
    # We use a nested class (enum) because this is specific to albums
    class Type(enum.Enum):
        ALBUM = "album"
        SINGLE = "single"
        COMPILATION = "compilation"

    album_type: Type = Column(Enum(Type), nullable=False)
    # Spotify doesnt always add the day or month to the release date:
    # https://developer.spotify.com/documentation/web-api/reference/#object-albumobject
    # So we have to store the month and day as 01 or something if release_date_precision isn't DAY
    release_date: datetime.date = Column(Date, nullable=False)

    class ReleaseDatePrecision(enum.Enum):
        YEAR = "year"
        MONTH = "month"
        DAY = "day"

    release_date_precision: ReleaseDatePrecision = Column(
        Enum(ReleaseDatePrecision), nullable=False, default=ReleaseDatePrecision.DAY
    )
    label: str = Column(Text, nullable=False)

    tracks: list[Track] = relationship(
        "Track",
        # https://github.com/sqlalchemy/sqlalchemy/issues/4708
        order_by="[Track.disc_number, Track.track_number]",
        back_populates="album",
    )
    artists: list[Artist] = relationship(
        "Artist",
        order_by="Artist.name",
        secondary=album_artist,
        back_populates="albums",
        cascade="all",
    )

    @classmethod
    def from_json(cls, album_json) -> Album:
        release_date_str = album_json["release_date"]
        release_date_precision = cls.ReleaseDatePrecision(
            album_json["release_date_precision"]
        )

        # If the precision isn't to the day, make sure the string is in a YYYY-MM-DD format
        if release_date_precision is cls.ReleaseDatePrecision.MONTH:
            # It doesn't matter what we add, so just add 01s
            release_date_str += "-01"
        elif release_date_precision is cls.ReleaseDatePrecision.YEAR:
            release_date_str += "-01-01"

        album = cls(
            id=album_json["id"],
            name=album_json["name"],
            popularity=album_json["popularity"],
            album_type=cls.Type(album_json["album_type"]),
            release_date=datetime.date.fromisoformat(release_date_str),
            release_date_precision=release_date_precision,
            label=album_json["label"],
        )

        album.genres = album_json["genres"]

        return album


class Track(SpotifyResource, Base):
    __tableargs__ = UniqueConstraint("album_id", "disc_number", "track_number")

    explicit: bool = Column(Boolean, nullable=False)
    duration_ms: int = Column(
        Integer, CheckConstraint("duration_ms > 0"), nullable=False
    )
    album_id: str = Column(ForeignKey("album.id"), nullable=False)
    disc_number: int = Column(
        Integer, CheckConstraint("disc_number > 0"), nullable=False, default=1
    )
    track_number: int = Column(
        Integer, CheckConstraint("track_number > 0"), nullable=False
    )
    is_playable: bool = Column(Boolean, nullable=False, default=True)

    album: Album = relationship(
        "Album",
        back_populates="tracks",
        # https://docs.sqlalchemy.org/en/13/orm/cascades.html
        cascade="all",
    )
    # https://docs.sqlalchemy.org/en/13/orm/extensions/orderinglist.html
    track_artists: list[TrackArtist] = relationship(
        "TrackArtist",
        order_by="TrackArtist.artist_order",
        collection_class=ordering_list("artist_order"),
        back_populates="track",
        cascade="all, delete-orphan",
    )
    # https://docs.sqlalchemy.org/en/13/orm/basic_relationships.html#one-to-one
    audio_features: AudioFeatures = relationship(
        "AudioFeatures",
        uselist=False,
        back_populates="track",
        cascade="all, delete-orphan",
    )

    # Adding a Artist() to this will create a new TrackArtist() with its .artist set
    # https://docs.sqlalchemy.org/en/13/orm/extensions/associationproxy.html#simplifying-association-objects
    artists: list[Artist] = association_proxy(
        "track_artists",
        "artist",
        # https://docs.sqlalchemy.org/en/13/orm/extensions/associationproxy.html#creation-of-new-values
        creator=TrackArtist.from_artist,
    )

    @classmethod
    def from_json(cls, track_json) -> Track:
        return cls(
            id=track_json["id"],
            name=track_json["name"],
            popularity=track_json["popularity"],
            explicit=track_json["explicit"],
            duration_ms=track_json["duration_ms"],
            disc_number=track_json["disc_number"],
            track_number=track_json["track_number"],
            is_playable=track_json["is_playable"],
        )


class AudioFeatures(Base):
    track_id: str = Column(ForeignKey("track.id"), primary_key=True)
    acousticness: float = Column(
        Float,
        CheckConstraint("acousticness BETWEEN 0 AND 1"),
        nullable=False,
    )
    danceability: float = Column(
        Float,
        CheckConstraint("danceability BETWEEN 0 AND 1"),
        nullable=False,
    )
    energy: float = Column(
        Float,
        CheckConstraint("energy BETWEEN 0 AND 1"),
        nullable=False,
    )
    instrumentalness: float = Column(
        Float,
        CheckConstraint("instrumentalness BETWEEN 0 AND 1"),
        nullable=False,
    )
    liveness: float = Column(
        Float,
        CheckConstraint("liveness BETWEEN 0 AND 1"),
        nullable=False,
    )
    speechiness: float = Column(
        Float,
        CheckConstraint("speechiness BETWEEN 0 AND 1"),
        nullable=False,
    )
    valence: float = Column(
        Float,
        CheckConstraint("valence BETWEEN 0 AND 1"),
        nullable=False,
    )
    loudness: float = Column(Float, nullable=False)
    # https://en.wikipedia.org/wiki/Pitch_class
    key: int = Column(
        Integer,
        CheckConstraint("key BETWEEN 0 AND 11"),
        nullable=False,
    )

    class Mode(enum.Enum):
        MINOR = 0
        MAJOR = 1

    mode: Mode = Column(Enum(Mode), nullable=False)
    tempo: float = Column(
        Float,
        CheckConstraint("tempo >= 0"),
        nullable=False,
    )
    time_signature: int = Column(
        Integer,
        CheckConstraint("time_signature >= 0"),
        nullable=False,
    )

    track: Track = relationship("Track", back_populates="audio_features")

    @classmethod
    def from_json(cls, audio_features_json) -> AudioFeatures:
        return cls(
            acousticness=audio_features_json["acousticness"],
            danceability=audio_features_json["danceability"],
            energy=audio_features_json["energy"],
            instrumentalness=audio_features_json["instrumentalness"],
            liveness=audio_features_json["liveness"],
            speechiness=audio_features_json["speechiness"],
            valence=audio_features_json["valence"],
            loudness=audio_features_json["loudness"],
            key=audio_features_json["key"],
            mode=cls.Mode(audio_features_json["mode"]),
            tempo=audio_features_json["tempo"],
            time_signature=audio_features_json["time_signature"],
        )


# The following classes make use of joined table inheritance
# This means you can access columns from their parent tables and SQLAlchemy
# does the rest with joins under the hood.

# TODO: Generalise from_unsaved methods into SpotifyResource?
class FollowedArtist(Artist):
    @classmethod
    def from_artist(cls, artist: Artist):
        followed_artist = cls(
            id=artist.id,
            name=artist.name,
            popularity=artist.popularity,
            followers=artist.followers,
        )

        return followed_artist

    @classmethod
    def from_json(cls, followed_artist_json) -> FollowedArtist:
        artist = Artist.from_json(followed_artist_json)

        return cls.from_artist(artist)


# Note that the enums from Album are the same for SavedAlbum AFAIK
# i.e. type(Album.ReleaseDatePrecision) is type(SavedAlbum.ReleaseDatePrecision)
class SavedAlbum(Album):
    added_at: datetime.datetime = Column(DateTime, nullable=False)

    @classmethod
    def from_album(cls, album: Album, added_at: datetime.datetime) -> SavedAlbum:
        saved_album = cls(
            id=album.id,
            name=album.name,
            popularity=album.popularity,
            album_type=album.album_type,
            release_date=album.release_date,
            release_date_precision=album.release_date_precision,
            label=album.label,
            added_at=added_at,
        )

        return saved_album

    @classmethod
    def from_json(cls, saved_album_json) -> SavedAlbum:
        added_at = isoparse(saved_album_json["added_at"])
        album = Album.from_json(saved_album_json["album"])

        return cls.from_album(album, added_at)


class SavedTrack(Track):
    added_at: datetime.datetime = Column(DateTime(timezone=True), nullable=False)

    @classmethod
    def from_track(cls, track: Track, added_at: datetime.datetime) -> SavedTrack:
        saved_track = cls(
            id=track.id,
            name=track.name,
            popularity=track.popularity,
            explicit=track.explicit,
            duration_ms=track.duration_ms,
            album_id=track.album_id,
            disc_number=track.disc_number,
            track_number=track.track_number,
            is_playable=track.is_playable,
            added_at=added_at,
        )

        return saved_track

    @classmethod
    def from_json(cls, saved_track_json) -> SavedAlbum:
        added_at = isoparse(saved_track_json["added_at"])
        track = Track.from_json(saved_track_json["track"])

        return cls.from_track(track, added_at)


# https://github.com/sqlalchemy/sqlalchemy/wiki/UniqueObjectValidatedOnPending
# See also https://docs.sqlalchemy.org/en/13/orm/session_events.html#session-lifecycle-events
@event.listens_for(Session, "transient_to_pending")
def validate_genre(session: Session, obj: HasGenres):
    """
    Receive the HasGenres object when it gets attached to a Session to correct
    its Genre objects.

    Note that this discards Genre objects part of the _genres collection on the object.
    """

    # We check the genres from HasGenres object so we can reassign the Genres to the correct ones
    if isinstance(obj, HasGenres) and obj._genres is not None:
        for index, genre in enumerate(obj._genres):
            if genre.id is None:
                # Make sure it's not going to be persisted before we check it
                if genre in session:
                    session.expunge(genre)

                obj._genres[index] = Genre.as_unique(session, genre.name)


def create_engine(db_file: str, **kwargs) -> Engine:
    # http://xion.io/post/code/sqlalchemy-regex-filters.html
    @event.listens_for(Engine, "connect")
    def sqlite_engine_connect(dbapi_connection: sqlite3.Connection, _):
        """
        Listener for the event of establishing connection to a SQLite database.

        Creates the regexp function for handling regular expressions
        within SQLite engine, pointing it to its Python implementation.
        """
        if not isinstance(dbapi_connection, sqlite3.Connection):
            return

        dbapi_connection.create_function(
            "regexp", 2, lambda x, y: 1 if re.search(x, y) else 0
        )

        # SQLite doesn't enforce foreign keys by default so we turn that on
        dbapi_connection.execute("PRAGMA foreign_keys = ON;")

    engine = sqlalchemy.create_engine(f"sqlite:///{db_file}", **kwargs)
    Base.metadata.create_all(engine)
    return engine


def reset_database(engine: Engine):
    """
    Drops all the tables from an engine and creates them again.
    """
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)