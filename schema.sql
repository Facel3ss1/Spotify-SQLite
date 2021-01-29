-- Based on https://developer.spotify.com/documentation/web-api/reference/#objects-index
-- This isn't directly used by the code, but it is an SQL version of the schema used by the database.

CREATE TABLE IF NOT EXISTS genre (
    id INTEGER NOT NULL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS artist (
    -- The ID refers to the Spotify ID (https://developer.spotify.com/documentation/web-api/#spotify-uris-and-ids) of that resource.
    -- They are in base62 (0-9a-zA-Z) and 22 chars long (https://stackoverflow.com/questions/37980664/spotify-track-id-features).
    -- https://developer.spotify.com/documentation/web-api/#spotify-uris-and-ids
    -- These can be used to generate URLs and URIs
    -- https://www.iana.org/assignments/uri-schemes/prov/spotify
    id VARCHAR(22) NOT NULL PRIMARY KEY,
    name TEXT NOT NULL,
    followers INTEGER NOT NULL CHECK (followers >= 0),
    popularity INTEGER NOT NULL CHECK (
        popularity BETWEEN 0 AND 100
    )
);

CREATE TABLE IF NOT EXISTS artist_genre (
    artist_id VARCHAR(22) NOT NULL,
    genre_id INTEGER NOT NULL,
    PRIMARY KEY (artist_id, genre_id),
    FOREIGN KEY (artist_id) REFERENCES artist (id),
    FOREIGN KEY (genre_id) REFERENCES genre (id)
);

CREATE TABLE IF NOT EXISTS album (
    id VARCHAR(22) NOT NULL PRIMARY KEY,
    name TEXT NOT NULL,
    album_type VARCHAR(11) NOT NULL CHECK (album_type IN ("ALBUM", "SINGLE", "COMPILATION")),
    -- Spotify doesnt always add the day or month to the release date, so we have a release_date_precision field to handle this.
    -- https://developer.spotify.com/documentation/web-api/reference/#object-albumobject
    -- So we have to store the month and day as 01 or something if release_date_precision isn't "DAY"
    -- because otherwise it wont be ISO 8601 compliant, which is bad for Python
    release_date DATE NOT NULL,
    release_date_precision VARCHAR(5) NOT NULL CHECK (
        release_date_precision IN ("YEAR", "MONTH", "DAY")
    ),
    label TEXT NOT NULL,
    popularity INTEGER NOT NULL CHECK (
        popularity BETWEEN 0 AND 100
    )
);

CREATE TABLE IF NOT EXISTS album_genre (
    album_id VARCHAR(22) NOT NULL,
    genre_id INTEGER NOT NULL,
    PRIMARY KEY (album_id, genre_id),
    FOREIGN KEY (album_id) REFERENCES album (id),
    FOREIGN KEY (genre_id) REFERENCES genre (id)
);

CREATE TABLE IF NOT EXISTS album_artist (
    album_id VARCHAR(22) NOT NULL,
    artist_id VARCHAR(22) NOT NULL,
    PRIMARY KEY (album_id, artist_id),
    FOREIGN KEY (album_id) REFERENCES album (id),
    FOREIGN KEY (artist_id) REFERENCES artist (id)
);

CREATE TABLE IF NOT EXISTS track (
    id VARCHAR(22) NOT NULL PRIMARY KEY,
    name TEXT NOT NULL,
    explicit BOOLEAN NOT NULL,
    duration_ms INTEGER NOT NULL CHECK (duration_ms > 0),
    album_id VARCHAR(22) NOT NULL,
    disc_number INTEGER NOT NULL CHECK (disc_number > 0),
    track_number INTEGER NOT NULL CHECK (track_number > 0),
    popularity INTEGER NOT NULL CHECK (
        popularity BETWEEN 0 AND 100
    ),
    is_playable BOOLEAN NOT NULL,
    UNIQUE (album_id, disc_number, track_number),
    FOREIGN KEY (album_id) REFERENCES album (id)
);

CREATE TABLE IF NOT EXISTS track_artist (
    track_id VARCHAR(22) NOT NULL,
    artist_id VARCHAR(22) NOT NULL,
    -- You can sort by this to get the order of the artists on the track
    artist_order INTEGER NOT NULL CHECK (artist_order >= 0),
    UNIQUE (track_id, artist_order),
    PRIMARY KEY (track_id, artist_id),
    FOREIGN KEY (track_id) REFERENCES track (id),
    FOREIGN KEY (artist_id) REFERENCES artist (id)
);

CREATE TABLE IF NOT EXISTS audio_features (
    track_id VARCHAR(22) NOT NULL PRIMARY KEY,
    acousticness FLOAT NOT NULL CHECK (
        acousticness BETWEEN 0 AND 1
    ),
    danceability FLOAT NOT NULL CHECK (
        danceability BETWEEN 0 AND 1
    ),
    energy FLOAT NOT NULL CHECK (
        energy BETWEEN 0 AND 1
    ),
    instrumentalness FLOAT NOT NULL CHECK (
        instrumentalness BETWEEN 0 AND 1
    ),
    liveness FLOAT NOT NULL CHECK (
        liveness BETWEEN 0 AND 1
    ),
    speechiness FLOAT NOT NULL CHECK (
        speechiness BETWEEN 0 AND 1
    ),
    valence FLOAT NOT NULL CHECK (
        valence BETWEEN 0 AND 1
    ),
    loudness FLOAT NOT NULL,
    -- https://en.wikipedia.org/wiki/Pitch_class
    key_pitch_class INTEGER NOT NULL CHECK (
        key_pitch_class BETWEEN 0 AND 11
    ),
    mode VARCHAR(5) NOT NULL CHECK (mode IN ("MINOR", "MAJOR")),
    tempo FLOAT NOT NULL CHECK (tempo >= 0),
    time_signature INTEGER NOT NULL CHECK (time_signature >= 0),
    FOREIGN KEY (track_id) REFERENCES track (id)
);

CREATE TABLE IF NOT EXISTS followed_artist (
    artist_id VARCHAR(22) NOT NULL PRIMARY KEY,
    FOREIGN KEY (artist_id) REFERENCES artist (id)
);

CREATE TABLE IF NOT EXISTS saved_album (
    album_id VARCHAR(22) NOT NULL PRIMARY KEY,
    added_at TIMESTAMP NOT NULL,
    FOREIGN KEY (album_id) REFERENCES album (id)
);

CREATE TABLE IF NOT EXISTS saved_track (
    track_id VARCHAR(22) NOT NULL PRIMARY KEY,
    added_at TIMESTAMP NOT NULL,
    FOREIGN KEY (track_id) REFERENCES track (id)
);