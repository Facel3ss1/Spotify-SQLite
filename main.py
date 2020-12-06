from __future__ import annotations

import asyncio
import logging
import os
from functools import partial
from math import ceil

from anyio import create_memory_object_stream, create_task_group
from dotenv import load_dotenv
from progress.bar import Bar
from sqlalchemy.orm import Session

import spotifysqlite.db as db
from spotifysqlite.api import SpotifySession, batcher

logger = logging.getLogger("spotifysqlite")
logger.setLevel(logging.DEBUG)


async def main():
    """
    `main` will authorise the user with Spotify and then fetch every track from their
    library. For each track in their library it will add these records to the database:
    - The `SavedTrack`
    - The track's `Album`
        - This includes the `Artist`s (and their respective `Genre`s) for that `Album`
        - The `Album` also has `Genre`s
    - The track's `Artist`s
        - Including the `Artist`'s `Genre`s
    - The `AudioFeatures` for that track

    `main` does this and ensures that there will be no duplicate records in the database.
    """
    # https://developer.spotify.com/documentation/general/guides/scopes/
    SCOPE = [
        "user-library-read",
        "user-follow-read",
        # "user-top-read",
        # "user-read-recently-played",
        # "playlist-read-private",
        # "playlist-read-collaborative",
    ]

    CLIENT_ID = os.getenv("CLIENT_ID")
    CLIENT_SECRET = os.getenv("CLIENT_SECRET")
    REDIRECT_URI = os.getenv("REDIRECT_URI")

    async with SpotifySession(
        CLIENT_ID, CLIENT_SECRET, scope=SCOPE, redirect_uri=REDIRECT_URI
    ) as spotify:
        await spotify.authorize_spotify()

        # 1. Fetch all the saved tracks and mark all the artists and albums as pending

        r = await spotify.get(
            "/v1/me/tracks", params={"limit": 1, "market": "from_token"}
        )
        paging_object = r.json()

        # The 'Get Saved Tracks' endpoint returns pages with a max size of 50 tracks
        PAGE_SIZE = 50

        # The total no. of tracks in the user's library
        total_tracks = paging_object["total"]
        # How many requests it will take to get all the tracks from the API
        total_pages = ceil(total_tracks / PAGE_SIZE)

        logger.info(f"Fetching {total_tracks} Tracks...")
        bar = Bar("Fetching Tracks...", max=total_tracks)

        # These all map IDs to the json response for that ID
        saved_track_jsons: dict[str, dict] = dict()
        album_jsons: dict[str, dict] = dict()
        artist_jsons: dict[str, dict] = dict()
        audio_features_jsons: dict[str, dict] = dict()

        pending_albums: set[str] = set()
        pending_artists: set[str] = set()

        tx_saved_track, rx_saved_track = create_memory_object_stream()

        async with create_task_group() as tg:
            async with tx_saved_track:
                # Fetch all the saved tracks
                for page in range(total_pages):
                    await tg.spawn(
                        partial(
                            spotify.get_saved_tracks,
                            tx_saved_track.clone(),
                            limit=PAGE_SIZE,
                            offset=page * PAGE_SIZE,
                        )
                    )

            async for saved_track_json in rx_saved_track:
                track = saved_track_json["track"]

                # Add the json response to saved_track_jsons
                track_id = track["id"]
                saved_track_jsons[track_id] = saved_track_json

                # Mark the albums and artists as pending
                album_id = track["album"]["id"]
                pending_albums.add(album_id)

                for artist in track["artists"]:
                    artist_id = artist["id"]
                    pending_artists.add(artist_id)

                bar.next()

        bar.finish()

        # 2. Fetch all the pending albums from step 1 and mark any new artists as pending
        logger.info(f"Fetching {len(pending_albums)} Albums...")
        bar = Bar("Fetching Albums...", max=len(pending_albums))

        tx_album_id, rx_album_id = create_memory_object_stream()
        tx_album, rx_album = create_memory_object_stream()

        async with create_task_group() as tg:
            async with tx_album:
                # Spawn a batcher which will request the albums in batches of 20
                await tg.spawn(
                    partial(
                        batcher,
                        rx_album_id,
                        tx_album.clone(),
                        lambda tx, b: spotify.get_multiple_albums(tx, ids=b),
                        batch_size=20,
                    )
                )

                async with tx_album_id:
                    while len(pending_albums) > 0:
                        album_id = pending_albums.pop()
                        await tx_album_id.send(album_id)

            async for album in rx_album:
                # Add the json response to album_jsons
                album_id = album["id"]
                album_jsons[album_id] = album

                # Mark the album's artists as pending if they aren't already
                for artist in album["artists"]:
                    artist_id = artist["id"]

                    if artist_id not in pending_artists:
                        pending_artists.add(artist_id)

                bar.next()

        bar.finish()

        # 3. Fetch all the pending artists from step 1 and 2
        logger.info(f"Fetching {len(pending_artists)} Artists...")
        bar = Bar("Fetching Artists...", max=len(pending_artists))

        tx_artist_id, rx_artist_id = create_memory_object_stream()
        tx_artist, rx_artist = create_memory_object_stream()

        async with create_task_group() as tg:
            async with tx_artist:
                await tg.spawn(
                    partial(
                        batcher,
                        rx_artist_id,
                        tx_artist.clone(),
                        lambda tx, b: spotify.get_multiple_artists(tx, ids=b),
                        batch_size=50,
                    )
                )

                async with tx_artist_id:
                    while len(pending_artists) > 0:
                        artist_id = pending_artists.pop()
                        await tx_artist_id.send(artist_id)

            async for artist in rx_artist:
                # Add the json response to artist_jsons
                artist_id = artist["id"]
                artist_jsons[artist_id] = artist

                bar.next()

        bar.finish()

        # 4. Fetch the Audio Features for each track
        logger.info("Fetching Audio Features...")
        bar = Bar("Fetching Audio Features...", max=len(saved_track_jsons))

        tx_track_id, rx_track_id = create_memory_object_stream()
        tx_audio_features, rx_audio_features = create_memory_object_stream()

        async with create_task_group() as tg:
            async with tx_audio_features:
                await tg.spawn(
                    partial(
                        batcher,
                        rx_track_id,
                        tx_audio_features.clone(),
                        lambda tx, b: spotify.get_multiple_audio_features(tx, ids=b),
                        batch_size=100,
                    )
                )

                async with tx_track_id:
                    for track_id in saved_track_jsons.keys():
                        await tx_track_id.send(track_id)

            async for audio_features in rx_audio_features:
                track_id = audio_features["id"]
                audio_features_jsons[track_id] = audio_features
                bar.next()

        bar.finish()

        # 5. Loop through the JSON responses to create and add the DB objects
        logger.info("Setting up Database...")
        print("Setting up Database...")

        filename = "test.db"

        while os.path.exists(filename):
            try:
                os.remove(filename)
                break
            except PermissionError:
                input(
                    f"{filename} is open in another program! Close it and press enter... "
                )
                continue

        engine = db.create_engine(filename)
        session = Session(engine)

        logger.info("Adding to Database...")
        bar = Bar("Adding to Database...", max=len(saved_track_jsons))

        # This maps the IDs to the database objects
        artists: dict[str, db.Artist] = dict()
        albums: dict[str, db.Album] = dict()

        for artist_json in artist_jsons.values():
            artist = db.Artist.from_json(artist_json)
            artists[artist.id] = artist

        for album_json in album_jsons.values():
            album = db.Album.from_json(album_json)

            # Add the artists for the album
            for artist_id in map(lambda a: a["id"], album_json["artists"]):
                artist = artists[artist_id]
                album.artists.append(artist)

            albums[album.id] = album

        for saved_track_json in saved_track_jsons.values():
            saved_track = db.SavedTrack.from_json(saved_track_json)

            # Add the audio features
            if saved_track.id in audio_features_jsons:
                audio_features_json = audio_features_jsons[saved_track.id]
                audio_features = db.AudioFeatures.from_json(audio_features_json)
                saved_track.audio_features = audio_features

            track_json = saved_track_json["track"]

            # Add the album
            album_id = track_json["album"]["id"]
            album = albums[album_id]
            saved_track.album = album

            # Add the artists for the track
            for artist_id in map(lambda a: a["id"], track_json["artists"]):
                artist = artists[artist_id]
                saved_track.artists.append(artist)

            session.add(saved_track)

            bar.next()

        bar.finish()

        print("Saving Database...")
        session.commit()


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
