# https://www.python.org/dev/peps/pep-0585/
# and https://www.python.org/dev/peps/pep-0563/
from __future__ import annotations

import datetime
import enum
import re
import sqlite3

import sqlalchemy
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
    inspect,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.ext.associationproxy import association_proxy
from sqlalchemy.ext.declarative import (
    as_declarative,
    declared_attr,
    has_inherited_table,
)
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.ext.orderinglist import ordering_list
from sqlalchemy.orm import (
    Session,
    backref,
    object_session,
    relationship,
    validates,
)
from sqlalchemy.orm.attributes import get_history

# https://docs.sqlalchemy.org/en/13/orm/tutorial.html#
# https://docs.sqlalchemy.org/en/13/orm/extensions/declarative/relationships.html

# TODO: Views?
# TODO: https://docs.sqlalchemy.org/en/13/orm/backref.html
# TODO: Sorting Artists with 'The' taken into account?
# TODO: https://click.palletsprojects.com/en/7.x/
# TODO: https://docs.python-guide.org/writing/structure/
# TODO: https://blog.jupyter.org/a-jupyter-kernel-for-sqlite-9549c5dcf551


@as_declarative()
class Base:
    """
    This is the Base class for the declaritive ORM in SQLAlchemy.

    The class name is converted from CamelCase to snake_case to derive ``__tablename__``
    and ``__repr__()`` is overridden to show the columns of the mapper.
    """

    # @declared_attr makes it a class property, so the first argument is the class, not self
    # https://stackoverflow.com/questions/1175208/elegant-python-function-to-convert-camelcase-to-snake-case
    @declared_attr
    def __tablename__(cls):
        name = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", cls.__name__)
        return re.sub("([a-z0-9])([A-Z])", r"\1_\2", name).lower()

    # https://stackoverflow.com/questions/2441796/how-to-discover-table-properties-from-sqlalchemy-mapped-object/2448930#2448930
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


# Based on https://developer.spotify.com/documentation/web-api/reference-beta/#objects-index

# https://docs.sqlalchemy.org/en/13/orm/extensions/declarative/mixins.html
class SpotifyResource:
    """
    Define a class that can be identified by a Spotify ID and defines hybrid properties
    for the URI and the URLs.

    Inheriting from a ``SpotifyResource`` creates the saved version of the resource.
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

    # TODO: Class property?
    @classmethod
    def resource_name(cls) -> str:
        """
        The ``__tablename__`` of the resource at the top of the inheritance hierarchy.

        e.g. This will return ``"track"`` for both ``Track`` and ``SavedTrack``
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


# TODO: Don't delete saved stuff in the orphan checks
# http://witkowskibartosz.com/blog/python_decorators_vs_inheritance.html
def auto_delete_orphans(*attr_keys):
    """
    Class decorator that will register orphan listeners on the attributes in
    ``attr_keys``.

    The orphan listeners will automatically delete orphaned objects from the attributes
    when an instance of the class is deleted.

    This is meant for many-to-many relationships, which can't have cascade rules.
    """

    def decorate(cls):
        # Based on https://sqlalchemy-utils.readthedocs.io/en/latest/_modules/sqlalchemy_utils/listeners.html#auto_delete_orphans
        @event.listens_for(cls, "attribute_instrument", propagate=True)
        def register_listener(_parent_class, _key, attr):
            # The parent_class is the class we've just deleted
            parent_class = attr.parent.class_
            key = attr.key
            # We only want to register the orphan listener on base classes
            if key in attr_keys and not has_inherited_table(parent_class):
                # The target_class is what we will try to delete orphans from
                target_class = attr.property.mapper.class_
                # TODO: backref?
                back_populates = attr.property.back_populates

                if not back_populates:
                    raise AttributeError(
                        f"The attribute {cls.__name__}.{key} given for auto_delete_orphans needs to have back_populates set."
                    )
                if isinstance(back_populates, tuple):
                    back_populates = back_populates[0]

                @event.listens_for(Session, "after_flush")
                def delete_orphan_listener(session: Session, ctx):
                    # Check in the session if we've deleted anything that would leave behind orphans
                    orphans_found = any(
                        isinstance(obj, parent_class) and get_history(obj, key).deleted
                        for obj in session.dirty
                    ) or any(isinstance(obj, parent_class) for obj in session.deleted)

                    if orphans_found:
                        # Emit a DELETE for all orphans that aren't saved
                        session.query(target_class).filter(
                            ~getattr(target_class, back_populates).any()
                        ).delete(synchronize_session=False)

        @event.listens_for(cls, "class_instrument", propagate=True)
        def recieve_class_instrument(inst_cls):
            if not has_inherited_table(inst_cls):
                # mapper = inspect(inst_cls)
                pass

        return cls

    return decorate


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
# We have to use a class instead of a secondary table
# because artist_order is an additional column
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

    @classmethod
    def from_artist(cls, artist: Artist):
        # The track_id will be added in due to its relationship with TrackArtist
        return cls(artist_id=artist.id)


# TODO: Index for name?
class Genre(Base):
    id: int = Column(Integer, primary_key=True)
    name: str = Column(Text(collation="nocase"), nullable=False, unique=True)
    # TODO: Add 'Sound of __' playlists?

    # https://github.com/sqlalchemy/sqlalchemy/wiki/UniqueObject
    @classmethod
    def as_unique(cls, session: Session, name: str):
        """
        Given a ``Session`` and the name of a genre, return the correct Genre object
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
# @auto_delete_orphans("_genres")
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
            backref=f"{tablename}s",
        )

    # https://docs.sqlalchemy.org/en/13/orm/extensions/associationproxy.html#simplifying-scalar-collections
    @declared_attr
    def genres(cls):
        return association_proxy("_genres", "name")

    @validates("_genres")
    def _validate_genre(self, key, genre):
        """
        Receive the event that occurs when someobject._genres is appended to.

        If the object is present in a Session, then make sure it's the Genre
        object that we looked up from the database.

        Otherwise, do nothing and we'll fix it in _validate_genre when the object is
        added to a Session.
        """
        sess = object_session(self)
        if sess is not None:
            return Genre.as_unique(sess, genre.name)
        else:
            return genre


class Artist(SpotifyResource, HasGenres, Base):
    followers: int = Column(Integer, CheckConstraint("followers >= 0"), nullable=False)
    popularity: int = Column(
        Integer, CheckConstraint("popularity BETWEEN 0 AND 100"), nullable=False
    )

    albums: list["Album"] = relationship(
        "Album",
        order_by="desc(Album.release_date)",
        secondary=album_artist,
        back_populates="artists",
    )
    track_artists: list[TrackArtist] = relationship("TrackArtist", backref="artist")
    # TODO: Is this useful?
    tracks = association_proxy("track_artists", "track")


class Album(SpotifyResource, HasGenres, Base):
    # We use a nested class (enum) because this is specific to albums
    class Type(enum.Enum):
        ALBUM = "album"
        SINGLE = "single"
        COMPILATION = "compilation"

    album_type: Type = Column(Enum(Type), nullable=False, default=Type.ALBUM)
    # Spotify doesnt always add the day or month to the release date:
    # https://developer.spotify.com/documentation/web-api/reference-beta/#object-albumobject
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
    popularity: int = Column(
        Integer, CheckConstraint("popularity BETWEEN 0 AND 100"), nullable=False
    )

    tracks: list["Track"] = relationship(
        "Track",
        # https://github.com/sqlalchemy/sqlalchemy/issues/4708
        order_by="[Track.disc_number, Track.track_number]",
        backref="album",
        # https://docs.sqlalchemy.org/en/13/orm/cascades.html
        cascade="all, delete-orphan",
    )
    artists: list[Artist] = relationship(
        "Artist",
        order_by="Artist.name",
        secondary=album_artist,
        back_populates="albums",
    )


class Track(SpotifyResource, Base):
    __tableargs__ = UniqueConstraint("album_id", "disc_number", "track_number")

    explicit: bool = Column(Boolean, nullable=False)
    duration_ms: int = Column(
        Integer, CheckConstraint("duration_ms > 0"), nullable=False
    )
    album_id: str = Column(ForeignKey("album.id"), nullable=False)
    # TODO: More robust checking of validity of disc and track numbers
    disc_number: int = Column(
        Integer, CheckConstraint("disc_number > 0"), nullable=False, default=1
    )
    track_number: int = Column(
        Integer, CheckConstraint("track_number > 0"), nullable=False
    )
    popularity: int = Column(
        Integer, CheckConstraint("popularity BETWEEN 0 AND 100"), nullable=False
    )
    is_playable: bool = Column(Boolean, nullable=False, default=True)

    # https://docs.sqlalchemy.org/en/13/orm/extensions/orderinglist.html
    track_artists: list[TrackArtist] = relationship(
        "TrackArtist",
        order_by="TrackArtist.artist_order",
        collection_class=ordering_list("artist_order"),
        backref="track",
    )
    # https://docs.sqlalchemy.org/en/13/orm/basic_relationships.html#one-to-one
    audio_features: "AudioFeatures" = relationship(
        "AudioFeatures",
        uselist=False,
        backref="track",
        cascade="all, delete-orphan",
    )

    # Adding a Artist() to artists will create a new TrackArtist() with its artist_id set
    # https://docs.sqlalchemy.org/en/13/orm/extensions/associationproxy.html#simplifying-association-objects
    artists: list[Artist] = association_proxy(
        "track_artists",
        "artist",
        # https://docs.sqlalchemy.org/en/13/orm/extensions/associationproxy.html#creation-of-new-values
        creator=TrackArtist.from_artist,
    )


class AudioFeatures(Base):
    track_id: str = Column(ForeignKey("track.id"), primary_key=True)
    # https://docs.microsoft.com/en-us/sql/t-sql/data-types/precision-scale-and-length-transact-sql?view=sql-server-ver15
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


# The following classes make use of joined table inheritance
# This means you can access columns from their parent tables and SQLAlchemy
# does the rest with joins under the hood.


# TODO: With that being said, single table inheritance might be better for this?
class FollowedArtist(Artist):
    pass


# Note that the enums from Album are the same for SavedAlbum afaik
# type(Album.ReleaseDatePrecision) is type(SavedAlbum.ReleaseDatePrecision)
class SavedAlbum(Album):
    added_at: datetime.datetime = Column(
        DateTime, nullable=False, default=datetime.datetime.now
    )


class SavedTrack(Track):
    added_at: datetime.datetime = Column(
        DateTime,
        nullable=False,
        # default=datetime.datetime.now
    )

    # TODO: Generalise this into SpotifyResource?
    # See also https://groups.google.com/g/sqlalchemy/c/F42nv5yA9rw?pli=1
    # See also https://github.com/zzzeek/test_sqlalchemy/issues/648
    @classmethod
    def from_track(cls, track: Track, added_at: datetime.datetime):
        # TODO: Use object_session to delete original track
        # Or use session.merge with SpotifyResources

        saved_track = cls(
            id=track.id,
            name=track.name,
            explicit=track.explicit,
            duration_ms=track.duration_ms,
            album_id=track.album_id,
            disc_number=track.disc_number,
            track_number=track.track_number,
            popularity=track.popularity,
            is_playable=track.is_playable,
            added_at=added_at,
        )

        return saved_track


# https://github.com/sqlalchemy/sqlalchemy/wiki/UniqueObjectValidatedOnPending
# See also https://docs.sqlalchemy.org/en/13/orm/session_events.html#session-lifecycle-events
@event.listens_for(Session, "transient_to_pending")
def validate_genre(session: Session, object_: HasGenres):
    """
    Receive the HasGenres object when it gets attached to a Session to correct
    its Genre objects.

    Note that this discards Genre objects part of the _genres collection on the object.
    """

    # We check the genres from HasGenres object so we can reassign the Genres to the correct ones
    if isinstance(object_, HasGenres) and object_._genres is not None:
        for index, genre in enumerate(object_._genres):
            if genre.id is None:
                # TODO: Genres with the same name are not being expunged?
                # Make sure it's not going to be persisted before we check it
                if genre in session:
                    session.expunge(genre)

                object_._genres[index] = Genre.as_unique(session, genre.name)


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

        # https://gist.github.com/eestrada/fd55398950c6ee1f1deb
        dbapi_connection.create_function(
            "regexp", 2, lambda x, y: 1 if re.search(x, y) else 0
        )

        # TODO: Foreign keys won't be enforced, we're not ready yet
        dbapi_connection.execute("PRAGMA foreign_keys = OFF;")

    engine = sqlalchemy.create_engine(f"sqlite:///{db_file}", **kwargs)
    Base.metadata.create_all(engine)
    return engine


if __name__ == "__main__":
    engine = create_engine(":memory:", echo=True)

    queen = Artist(
        id="1dfeR4HaWDbWqFHLkxsg1d", name="Queen", followers=745673, popularity=100
    )
    queen.genres = ["rock", "rock opera"]

    mgmt = FollowedArtist(
        id="0SwO7SWeDHJijQ3XNS7xEE", name="MGMT", followers=47243, popularity=100
    )
    mgmt.genres = ["rock", "modern rock"]

    a_night_at_the_opera = Album(
        id="1GbtB4zTqAsyfZEsm1RZfx",
        name="A Night at the Opera",
        release_date=datetime.date.fromisoformat("1975-01-01"),
        release_date_precision=Album.ReleaseDatePrecision.YEAR,
        label="Queen Productions Ltd",
        popularity=100,
    )

    a_night_at_the_opera.artists.append(queen)
    a_night_at_the_opera.genres.append("rock")

    bohemian_rhapsody = SavedTrack(
        id="4u7EnebtmKWzUH433cf5Qv",
        name="Bohemian Rhapsody",
        explicit=False,
        duration_ms=10000,
        track_number=11,
        popularity=100,
    )

    bohemian_rhapsody.album = a_night_at_the_opera
    bohemian_rhapsody.artists.append(queen)

    session = Session(engine)
    session.add_all([mgmt, bohemian_rhapsody])
    session.commit()

    session.delete(mgmt)
    session.commit()
    #
