-- Based on https://developer.spotify.com/documentation/web-api/reference-beta/#objects-index
-- TODO: https://www.sqlite.org/withoutrowid.html
-- TODO: Views for saved/following
-- TODO: Generated columns for URIs/URLs (requires SQLite 3.31.0) https://www.sqlite.org/gencol.html
-- And for duration strings like "5:21"
-- The concat op for strings in SQLite is ||
-- TODO: FTS5? Use trigger to insert into virtual table https://www.sqlitetutorial.net/sqlite-full-text-search/
-- See also https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.enable_load_extension
-- TODO: Fix regex for ids in DBeaver
-- TODO: Make fields not part of simplified objects nullable?
CREATE TABLE IF NOT EXISTS genre (
    id INTEGER NOT NULL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE -- TODO: Add "Sound of ..." playlists?
);
CREATE TABLE IF NOT EXISTS artist (
    -- The IDs refer to the Spotify ID of that resource
    -- https://developer.spotify.com/documentation/web-api/#spotify-uris-and-ids
    -- These can be used to generate URLs and URIs with generated columns (see TODO)
    -- https://www.iana.org/assignments/uri-schemes/prov/spotify
    id TEXT NOT NULL PRIMARY KEY,
    name TEXT NOT NULL,
    followers INTEGER NOT NULL CHECK (followers >= 0),
    popularity INTEGER NOT NULL CHECK (
        popularity BETWEEN 0 AND 100
    )
);
CREATE TABLE IF NOT EXISTS artist_genre (
    artist_id TEXT NOT NULL,
    genre_id INTEGER NOT NULL,
    PRIMARY KEY (artist_id, genre_id),
    FOREIGN KEY (artist_id) REFERENCES artist (id) ON DELETE CASCADE ON UPDATE CASCADE,
    -- This is a deferred foreign key, see track for an explaination
    FOREIGN KEY (genre_id) REFERENCES genre (id) ON DELETE RESTRICT ON UPDATE CASCADE DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE IF NOT EXISTS album (
    id TEXT NOT NULL PRIMARY KEY,
    name TEXT NOT NULL,
    -- TODO: Enum?
    album_type TEXT NOT NULL CHECK (album_type IN ("album", "single", "compilation")),
    -- If SQLite doesn't recognise the type name, it defaults to the NUMERIC type affinity:
    -- https://www.sqlite.org/datatype3.html#determination_of_column_affinity
    -- However Python automatically treats DATE as a datetime.date for us:
    -- https://docs.python.org/3/library/sqlite3.html#default-adapters-and-converters
    -- But Spotify doesnt always add the day or month to the release date:
    -- https://developer.spotify.com/documentation/web-api/reference-beta/#object-albumobject
    -- So we have to store the month and day as 01 or something if release_date_precision isn't "day"
    -- because otherwise it wont be ISO 8601 compliant, which is bad for Python
    release_date DATE NOT NULL,
    -- TODO: Enum?
    release_date_precision TEXT NOT NULL CHECK (
        release_date_precision IN ("year", "month", "day")
    ),
    label TEXT NOT NULL,
    popularity INTEGER NOT NULL CHECK (
        popularity BETWEEN 0 AND 100
    )
);
CREATE TABLE IF NOT EXISTS album_genre (
    album_id TEXT NOT NULL,
    genre_id INTEGER NOT NULL,
    PRIMARY KEY (album_id, genre_id),
    FOREIGN KEY (album_id) REFERENCES album (id) ON DELETE CASCADE ON UPDATE CASCADE,
    FOREIGN KEY (genre_id) REFERENCES genre (id) ON DELETE RESTRICT ON UPDATE CASCADE DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE IF NOT EXISTS album_artist (
    album_id TEXT NOT NULL,
    artist_id TEXT NOT NULL,
    PRIMARY KEY (album_id, artist_id),
    FOREIGN KEY (album_id) REFERENCES album (id) ON DELETE CASCADE ON UPDATE CASCADE,
    FOREIGN KEY (artist_id) REFERENCES artist (id) ON DELETE RESTRICT ON UPDATE CASCADE DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE IF NOT EXISTS track (
    id TEXT NOT NULL PRIMARY KEY,
    name TEXT NOT NULL,
    -- In SQLite, BOOLEAN defaults to the NUMERIC type affinity (its an unrecognised type name)
    -- However we use adapters and converters in python to interpret this correctly
    -- https://stackoverflow.com/questions/16936608/storing-bools-in-sqlite-database
    explicit BOOLEAN NOT NULL,
    duration_ms INTEGER NOT NULL CHECK (duration_ms > 0),
    album_id TEXT NOT NULL,
    disc_number INTEGER NOT NULL CHECK (disc_number > 0),
    track_number INTEGER NOT NULL CHECK (track_number > 0),
    popularity INTEGER NOT NULL CHECK (
        popularity BETWEEN 0 AND 100
    ),
    is_playable BOOLEAN NOT NULL,
    -- This is a deferred foreign key so we can add the track first, 
    -- then the album, then the artists etc. without foreign key complaints
    -- As long as the constraints are upheld at commit time, its fine.
    -- https://www.sqlite.org/foreignkeys.html#fk-deferred
    -- This means we have to turn off autocommit:
    -- https://docs.python.org/3/library/sqlite3.html#controlling-transactions
    UNIQUE (album_id, disc_number, track_number),
    FOREIGN KEY (album_id) REFERENCES album (id) ON DELETE RESTRICT ON UPDATE CASCADE DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE IF NOT EXISTS track_artist (
    track_id TEXT NOT NULL,
    artist_id TEXT NOT NULL,
    -- You can sort by this to get the order of the artists on the track
    artist_order INTEGER NOT NULL CHECK (artist_order >= 0),
    UNIQUE (track_id, artist_order),
    PRIMARY KEY (track_id, artist_id),
    FOREIGN KEY (track_id) REFERENCES track (id) ON DELETE CASCADE ON UPDATE CASCADE,
    FOREIGN KEY (artist_id) REFERENCES artist (id) ON DELETE RESTRICT ON UPDATE CASCADE DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE IF NOT EXISTS audio_features (
    track_id TEXT NOT NULL PRIMARY KEY,
    acousticness REAL NOT NULL CHECK (
        acousticness BETWEEN 0 AND 1
    ),
    danceability REAL NOT NULL CHECK (
        danceability BETWEEN 0 AND 1
    ),
    energy REAL NOT NULL CHECK (
        energy BETWEEN 0 AND 1
    ),
    instrumentalness REAL NOT NULL CHECK (
        instrumentalness BETWEEN 0 AND 1
    ),
    liveness REAL NOT NULL CHECK (
        liveness BETWEEN 0 AND 1
    ),
    speechiness REAL NOT NULL CHECK (
        speechiness BETWEEN 0 AND 1
    ),
    valence REAL NOT NULL CHECK (
        valence BETWEEN 0 AND 1
    ),
    loudness REAL NOT NULL,
    -- https://en.wikipedia.org/wiki/Pitch_class
    key_pitch_class INTEGER NOT NULL CHECK (
        key_pitch_class BETWEEN 0 AND 11
    ),
    mode INTEGER NOT NULL CHECK (
        mode = 0 -- Minor
        OR mode = 1 -- Major
    ),
    tempo REAL NOT NULL CHECK (tempo > 0),
    time_signature INTEGER NOT NULL CHECK (time_signature > 0),
    FOREIGN KEY (track_id) REFERENCES track (id) ON DELETE CASCADE ON UPDATE CASCADE
);
CREATE TABLE IF NOT EXISTS followed_artist (
    artist_id TEXT NOT NULL PRIMARY KEY,
    FOREIGN KEY (artist_id) REFERENCES artist (id) ON DELETE CASCADE ON UPDATE CASCADE DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE IF NOT EXISTS saved_album (
    album_id TEXT NOT NULL PRIMARY KEY,
    -- See release_date on album for an explaination
    added_at TIMESTAMP NOT NULL,
    FOREIGN KEY (album_id) REFERENCES album (id) ON DELETE CASCADE ON UPDATE CASCADE DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE IF NOT EXISTS saved_track (
    track_id TEXT NOT NULL PRIMARY KEY,
    added_at TIMESTAMP NOT NULL,
    FOREIGN KEY (track_id) REFERENCES track (id) ON DELETE CASCADE ON UPDATE CASCADE DEFERRABLE INITIALLY DEFERRED
);
-- TODO:
-- These triggers will delete any genres, artists or albums that aren't being referenced anymore
-- The delete cascades from the tracks/albums will run these
CREATE TRIGGER IF NOT EXISTS delete_orphaned_genres_after_delete_artist_genre
AFTER DELETE ON artist_genre BEGIN
DELETE FROM genre
WHERE EXISTS (
        SELECT genre.id
        FROM genre
            LEFT JOIN artist_genre ON artist_genre.genre_id = genre.id
            LEFT JOIN album_genre ON album_genre.genre_id = genre.id
        WHERE genre.id = old.genre_id
            AND artist_genre.artist_id IS NULL
            AND album_genre.album_id IS NULL
    );
END;
CREATE TRIGGER IF NOT EXISTS delete_orphaned_genres_after_delete_album_genre
AFTER DELETE ON album_genre BEGIN
DELETE FROM genre
WHERE EXISTS (
        SELECT genre.id
        FROM genre
            LEFT JOIN artist_genre ON artist_genre.genre_id = genre.id
            LEFT JOIN album_genre ON album_genre.genre_id = genre.id
        WHERE genre.id = old.genre_id
            AND artist_genre.artist_id IS NULL
            AND album_genre.album_id IS NULL
    );
END;
CREATE TRIGGER IF NOT EXISTS delete_orphaned_artists_after_delete_album_artist
AFTER DELETE ON album_artist BEGIN
DELETE FROM artist
WHERE EXISTS (
        SELECT artist.id
        FROM artist
            LEFT JOIN album_artist ON album_artist.artist_id = artist.id
            LEFT JOIN track_artist ON track_artist.artist_id = artist.id
        WHERE artist.id = old.artist_id
            AND album_artist.artist_id IS NULL
            AND track_artist.artist_id IS NULL
    );
END;
CREATE TRIGGER IF NOT EXISTS delete_orphaned_artists_after_delete_track_artist
AFTER DELETE ON track_artist BEGIN
DELETE FROM artist
WHERE EXISTS (
        SELECT artist.id
        FROM artist
            LEFT JOIN album_artist ON album_artist.artist_id = artist.id
            LEFT JOIN track_artist ON track_artist.artist_id = artist.id
        WHERE artist.id = old.artist_id
            AND album_artist.artist_id IS NULL
            AND track_artist.artist_id IS NULL
    );
END;
CREATE TRIGGER IF NOT EXISTS delete_orphaned_albums_after_delete_track
AFTER DELETE ON track BEGIN
DELETE FROM album
WHERE EXISTS (
        SELECT album.id
        FROM album
            LEFT JOIN track ON track.album_id = album.id
        WHERE album.id = old.album_id
            AND track.album_id IS NULL
    );
END;