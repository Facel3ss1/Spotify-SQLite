from __future__ import annotations

import asyncio
import logging
import os
from functools import cache, partial
from math import ceil
from typing import Callable, Coroutine, Union

from anyio import create_memory_object_stream, create_task_group
from anyio.streams.memory import MemoryObjectSendStream
from dotenv import load_dotenv
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from tqdm.asyncio import tqdm

import spotifysqlite.api as api
import spotifysqlite.db as db

logger = logging.getLogger("spotifysqlite")
logger.setLevel(logging.DEBUG)


# TODO: https://typer.tiangolo.com/
# TODO: Syncing algorithm
# TODO: Playlist support?


class Response:
    _json: dict
    _is_saved: bool

    @property
    def is_saved(self):
        return self._is_saved

    def __init__(self, json: dict, is_saved: bool) -> None:
        self._json = json
        self._is_saved = is_saved

    def to_db_object():
        raise NotImplementedError

    @property
    def id() -> str:
        raise NotImplementedError

    @staticmethod
    def set_of_ids(responses: list[Response]) -> set[str]:
        return set(map(lambda r: r.id, responses))


class TrackResponse(Response):
    @cache
    def to_db_object(self) -> Union[db.Track, db.SavedTrack]:
        if self._is_saved:
            return db.SavedTrack.from_json(self._json)
        else:
            return db.Track.from_json(self._json)

    @property
    def _track_json(self) -> dict:
        if self._is_saved:
            return self._json["track"]
        else:
            return self._json

    @property
    def id(self) -> str:
        return self._track_json["id"]

    def required_artists(self) -> list[str]:
        return list(map(lambda a: a["id"], self._track_json["artists"]))

    def required_album(self) -> str:
        return self._track_json["album"]["id"]


class ArtistResponse(Response):
    @cache
    def to_db_object(self) -> Union[db.Artist, db.FollowedArtist]:
        if self._is_saved:
            return db.FollowedArtist.from_json(self._json)
        else:
            return db.Artist.from_json(self._json)

    @property
    def id(self) -> str:
        return self._json["id"]


class AlbumResponse(Response):
    @cache
    def to_db_object(self) -> Union[db.Album, db.FollowedArtist]:
        if self._is_saved:
            return db.SavedAlbum.from_json(self._json)
        else:
            return db.Album.from_json(self._json)

    @property
    def _album_json(self) -> dict:
        if self._is_saved:
            return self._json["album"]
        else:
            return self._json

    @property
    def id(self) -> str:
        return self._album_json["id"]

    def required_artists(self) -> list[str]:
        return list(map(lambda a: a["id"], self._album_json["artists"]))


class SpotifyDownloader:
    # https://developer.spotify.com/documentation/general/guides/scopes/
    SCOPE = [
        "user-library-read",
        "user-follow-read",
        # "user-top-read",
        # "user-read-recently-played",
        # "playlist-read-private",
        # "playlist-read-collaborative",
    ]

    spotify: api.SpotifySession
    is_authorized: bool

    def __init__(
        self, *, client_id: str, client_secret: str, redirect_uri: str
    ) -> None:
        self.spotify = api.SpotifySession(
            client_id,
            client_secret,
            scope=SpotifyDownloader.SCOPE,
            redirect_uri=redirect_uri,
        )

        self.is_authorized = False

    async def __aenter__(self):
        if not self.is_authorized:
            await self.spotify.authorize_spotify()
            self.is_authorized = True

        await self.spotify.__aenter__()
        return self

    async def __aexit__(self, type, value, traceback):
        await self.spotify.__aexit__(type, value, traceback)

    async def fetch_saved_tracks(self) -> list[TrackResponse]:
        PAGE_SIZE = 50

        r = await self.spotify.get(
            "/v1/me/tracks", params={"limit": 1, "market": "from_token"}
        )

        paging_object = r.json()
        # The total no. of tracks in the user's library
        total_tracks = paging_object["total"]
        # How many requests it will take to get all the tracks from the API
        total_pages = ceil(total_tracks / PAGE_SIZE)

        tx_saved_track, rx_saved_track = create_memory_object_stream()

        async with create_task_group() as tg:
            async with tx_saved_track:
                # Fetch all the saved tracks
                for page in range(total_pages):
                    await tg.spawn(
                        partial(
                            self.spotify.get_saved_tracks,
                            tx_saved_track.clone(),
                            limit=PAGE_SIZE,
                            offset=page * PAGE_SIZE,
                        )
                    )

            return [
                TrackResponse(t, True)
                async for t in tqdm(
                    rx_saved_track,
                    ascii=True,
                    desc="Fetching Saved Tracks",
                    total=total_tracks,
                    unit="track",
                )
            ]

    async def fetch_saved_albums(self) -> list[AlbumResponse]:
        PAGE_SIZE = 50

        r = await self.spotify.get(
            "/v1/me/albums", params={"limit": 1, "market": "from_token"}
        )

        paging_object = r.json()
        total_albums = paging_object["total"]
        total_pages = ceil(total_albums / PAGE_SIZE)

        tx_saved_album, rx_saved_album = create_memory_object_stream()

        async with create_task_group() as tg:
            async with tx_saved_album:
                # Fetch all the saved tracks
                for page in range(total_pages):
                    await tg.spawn(
                        partial(
                            self.spotify.get_saved_albums,
                            tx_saved_album.clone(),
                            limit=PAGE_SIZE,
                            offset=page * PAGE_SIZE,
                        )
                    )

            return [
                AlbumResponse(a, True)
                async for a in tqdm(
                    rx_saved_album,
                    ascii=True,
                    desc="Fetching Saved Albums",
                    total=total_albums,
                    unit="album",
                )
            ]

    async def fetch_followed_artists(self) -> list[ArtistResponse]:
        PAGE_SIZE = 50

        r = await self.spotify.get(
            "/v1/me/following", params={"type": "artist", "limit": 1}
        )

        paging_object = r.json()
        total_artists = paging_object["artists"]["total"]

        artists: list[ArtistResponse] = list()
        after: str = None

        with tqdm(
            desc="Fetching Followed Artists",
            ascii=True,
            total=total_artists,
            unit="artist",
        ) as pbar:
            while len(artists) < total_artists:
                page = await self.spotify.get_followed_artists(
                    limit=PAGE_SIZE, after=after
                )
                artists.extend(map(lambda a: ArtistResponse(a, True), page))
                after = artists[-1].id

                pbar.update(len(page))

        return artists

    @staticmethod
    async def batcher(
        items: list,
        tx_out: MemoryObjectSendStream,
        batch_coro: Callable[[MemoryObjectSendStream, list], Coroutine],
        *,
        batch_size: int,
    ):
        """
        Takes a list of items, and splits it up into batches of size `batch_size`,
        then the batches are processed using `batch_coro` and the processed items are sent
        back using the `tx_out` stream.

        For each batch, `batch_coro` is spawned, and it takes the batch and a send stream as
        arguments. `batch_coro` is expected to process the batch, and then send back the
        processed items induvidually using the send stream. These processed items are then
        recieved by the batcher and finally sent back to the caller.
        """

        batches: list[list] = [
            items[offset : offset + batch_size]
            for offset in range(0, len(items), batch_size)
        ]

        async with create_task_group() as tg:
            async with tx_out:
                for batch in batches:
                    await tg.spawn(batch_coro, tx_out.clone(), batch)

    async def fetch_multiple_albums(self, ids: set[str]) -> list[AlbumResponse]:
        PAGE_SIZE = 20

        tx_album, rx_album = create_memory_object_stream()

        async with create_task_group() as tg:
            async with tx_album:
                await tg.spawn(
                    partial(
                        self.batcher,
                        list(ids),
                        tx_album.clone(),
                        lambda tx, b: self.spotify.get_multiple_albums(tx, ids=b),
                        batch_size=PAGE_SIZE,
                    )
                )

            return [
                AlbumResponse(a, False)
                async for a in tqdm(
                    rx_album,
                    ascii=True,
                    desc="Fetching Albums",
                    total=len(ids),
                    unit="album",
                )
            ]

    async def fetch_multiple_artists(self, ids: set[str]) -> list[ArtistResponse]:
        PAGE_SIZE = 50

        tx_artist, rx_artist = create_memory_object_stream()

        async with create_task_group() as tg:
            async with tx_artist:
                await tg.spawn(
                    partial(
                        self.batcher,
                        list(ids),
                        tx_artist.clone(),
                        lambda tx, b: self.spotify.get_multiple_artists(tx, ids=b),
                        batch_size=PAGE_SIZE,
                    )
                )

            return [
                ArtistResponse(a, False)
                async for a in tqdm(
                    rx_artist,
                    ascii=True,
                    desc="Fetching Artists",
                    total=len(ids),
                    unit="artist",
                )
            ]

    async def fetch_multiple_audio_features(
        self, ids: set[str]
    ) -> dict[str, db.AudioFeatures]:
        PAGE_SIZE = 100

        tx_audio_features, rx_audio_features = create_memory_object_stream()

        async with create_task_group() as tg:
            async with tx_audio_features:
                await tg.spawn(
                    partial(
                        self.batcher,
                        list(ids),
                        tx_audio_features.clone(),
                        lambda tx, b: self.spotify.get_multiple_audio_features(
                            tx, ids=b
                        ),
                        batch_size=PAGE_SIZE,
                    )
                )

            return {
                a["id"]: db.AudioFeatures.from_json(a)
                async for a in tqdm(
                    rx_audio_features,
                    ascii=True,
                    desc="Fetching Audio Features",
                    total=len(ids),
                    unit="af",
                )
            }

    @staticmethod
    def all_required_artists(
        responses: Union[list[TrackResponse], list[AlbumResponse]]
    ) -> set[str]:
        artists = set()

        for res in responses:
            for artist in res.required_artists():
                artists.add(artist)

        return artists

    @staticmethod
    def all_required_albums(responses: list[TrackResponse]) -> set[str]:
        albums = set()

        for res in responses:
            albums.add(res.required_album())

        return albums


class Program:
    dl: SpotifyDownloader
    engine: Engine

    tracks: dict[str, TrackResponse]
    albums: dict[str, AlbumResponse]
    artists: dict[str, ArtistResponse]
    audio_features: dict[str, db.AudioFeatures]

    def __init__(self, filename: str) -> None:
        CLIENT_ID = os.getenv("CLIENT_ID")
        CLIENT_SECRET = os.getenv("CLIENT_SECRET")
        REDIRECT_URI = os.getenv("REDIRECT_URI")

        if CLIENT_ID is None:
            raise Exception("CLIENT_ID not found in environment")

        if CLIENT_SECRET is None:
            raise Exception("CLIENT_SECRET not found in environment")

        if REDIRECT_URI is None:
            raise Exception("REDIRECT_URI not found in environment")

        self.dl = SpotifyDownloader(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            redirect_uri=REDIRECT_URI,
        )

        self.engine = db.create_engine(filename)

        self.tracks = dict()
        self.albums = dict()
        self.artists = dict()
        self.audio_features = dict()

    async def fetch_library(self):
        async with self.dl:
            # Fetch the user's library
            saved_tracks = await self.dl.fetch_saved_tracks()
            saved_albums = await self.dl.fetch_saved_albums()
            followed_artists = await self.dl.fetch_followed_artists()

            self.tracks: dict[str, TrackResponse] = {t.id: t for t in saved_tracks}
            self.albums: dict[str, AlbumResponse] = {a.id: a for a in saved_albums}
            self.artists: dict[str, ArtistResponse] = {
                a.id: a for a in followed_artists
            }

        await self.fetch_required()

        print("Setting up Database...")

        # Delete anything that was there before
        db.reset_database(self.engine)

        self.save_to_db()

    async def fetch_required(self):
        """
        `fetch_required` will authorise the user with Spotify and then fetch every track from their
        library. For each track in their library it will add these records to the database:
        - The `SavedTrack`
        - The track's `Album`
            - This includes the `Artist`s (and their respective `Genre`s) for that `Album`
            - The `Album` also has `Genre`s
        - The track's `Artist`s
            - Including the `Artist`'s `Genre`s
        - The `AudioFeatures` for that track

        Note that this deletes whatever was in the database beforehand.
        """

        async with self.dl:
            # Fetch the required albums from the saved tracks
            required_albums: set[str] = (
                self.dl.all_required_albums(self.tracks.values()) - self.albums.keys()
            )
            fetched_albums = await self.dl.fetch_multiple_albums(required_albums)
            self.albums |= {a.id: a for a in fetched_albums}

            # Fetch the required artists from the saved tracks, the saved albums, and the fetched albums
            required_artists: set[str] = (
                self.dl.all_required_artists(self.tracks.values())
                | self.dl.all_required_artists(self.albums.values())
            ) - self.artists.keys()
            fetched_artists = await self.dl.fetch_multiple_artists(required_artists)
            self.artists |= {a.id: a for a in fetched_artists}

            # TODO: Fetching the audio features can be done in parallel
            self.audio_features = await self.dl.fetch_multiple_audio_features(
                self.tracks.keys()
            )

    def save_to_db(self):
        session = Session(self.engine)

        # Add artists to the albums
        for album in self.albums.values():
            album_db = album.to_db_object()

            album_db.artists = list(
                map(lambda a: self.artists[a].to_db_object(), album.required_artists())
            )

        # TODO: I forgot to save the saved albums and followed artists whoops

        # Add albums, artists, and audio features to the tracks, and add the tracks to the session
        for track in tqdm(
            self.tracks.values(),
            ascii=True,
            desc="Adding Tracks to Database",
            unit="track",
        ):
            track_db = track.to_db_object()

            track_db.album = self.albums[track.required_album()].to_db_object()

            track_db.artists = list(
                map(lambda a: self.artists[a].to_db_object(), track.required_artists())
            )

            # TODO: If?
            track_db.audio_features = self.audio_features[track.id]

            with session.no_autoflush:
                session.add(track_db)

        print("Saving Database...")
        session.commit()


async def main():
    # TODO: Make this the Spotify username
    filename = "test.db"

    program = Program(filename)

    await program.fetch_library()


if __name__ == "__main__":
    # https://github.com/encode/httpx/issues/914
    import sys

    if (
        sys.version_info[0] == 3
        and sys.version_info[1] >= 8
        and sys.platform.startswith("win")
    ):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    # https://docs.python.org/3/howto/logging.html
    # logging.basicConfig(
    #     filename="debug.log",
    #     level=logging.DEBUG,
    #     format="%(levelname)s [%(asctime)s] %(name)s - %(message)s",
    #     datefmt="%Y-%m-%d %H:%M:%S",
    # )

    # logging.getLogger("sqlalchemy.engine").setLevel(logging.INFO)

    try:
        load_dotenv()
        asyncio.run(main())
        logger.info("Finished!")
        print("Finished!")
    except Exception as e:
        logger.exception("Exception occurred:")
        if e is not KeyboardInterrupt:
            raise e
