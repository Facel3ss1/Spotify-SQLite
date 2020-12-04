from __future__ import annotations

import asyncio
import logging
import os
from functools import partial
from math import ceil

import anyio
from anyio import create_memory_object_stream, create_task_group
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from dateutil.parser import isoparse
from dotenv import load_dotenv
from progress.bar import Bar
from sqlalchemy.orm import Session

import spotifysqlite.db as db
from spotifysqlite.api import SpotifySession, batcher

logger = logging.getLogger("spotifysqlite")
logger.setLevel(logging.DEBUG)


def track_from_json(track_json):
    return db.Track(
        id=track_json["id"],
        name=track_json["name"],
        explicit=track_json["explicit"],
        duration_ms=track_json["duration_ms"],
        album_id=track_json["album"]["id"],
        disc_number=track_json["disc_number"],
        track_number=track_json["track_number"],
        popularity=track_json["popularity"],
        is_playable=track_json["is_playable"],
    )


def saved_track_from_json(saved_track_json):
    added_at = isoparse(saved_track_json["added_at"])
    track = track_from_json(saved_track_json["track"])

    return db.SavedTrack.from_track(track, added_at)


def audio_features_from_json(audio_features_json):
    return db.AudioFeatures(
        acousticness=audio_features_json["acousticness"],
        danceability=audio_features_json["danceability"],
        energy=audio_features_json["energy"],
        instrumentalness=audio_features_json["instrumentalness"],
        liveness=audio_features_json["liveness"],
        speechiness=audio_features_json["speechiness"],
        valence=audio_features_json["valence"],
        loudness=audio_features_json["loudness"],
        key=audio_features_json["key"],
        mode=db.AudioFeatures.Mode(audio_features_json["mode"]),
        tempo=audio_features_json["tempo"],
        time_signature=audio_features_json["time_signature"],
    )


async def main():
    """
    `main` will authorise the user with Spotify and then fetch every track from their
    library. For each track in their library it will add these records to the database:
    - The `SavedTrack`
    - The track's `Artist`s
        - Including the `Artist`'s `Genre`s
    - The track's `Album`
        - This includes the `Artist`s (and their respective `Genre`s) for that `Album`
        - The `Album` also has `Genre`s
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
        saved_tracks: dict[str, dict] = dict()
        albums: dict[str, dict] = dict()
        artists: dict[str, dict] = dict()
        audio_features_dict: dict[str, dict] = dict()

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

                # Add the json response to saved_tracks
                track_id = track["id"]
                saved_tracks[track_id] = saved_track_json

                # Mark the albums and artists as pending
                album_id = track["album"]["id"]
                pending_albums.add(album_id)

                for artist in track["artists"]:
                    artist_id = artist["id"]
                    pending_artists.add(artist_id)

                bar.next()

        bar.finish()

        # 2. Fetch all the pending albums from step 1 and mark any new artists as pending
        # logger.info(f"Fetching {len(pending_albums)} Albums...")

        # 3. Fetch all the pending artists from step 1 and 2
        # logger.info(f"Fetching {len(pending_artists)} Artists...")

        # We can also fetch all the audio features in parallel with steps 2 and 3

        logger.info("Fetching Audio Features...")
        bar = Bar("Fetching Audio Features...", max=len(saved_tracks))

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
                    for track_id in saved_tracks.keys():
                        await tx_track_id.send(track_id)

            async for audio_features in rx_audio_features:
                track_id = audio_features["id"]
                audio_features_dict[track_id] = audio_features
                bar.next()

        bar.finish()

        # 4. Loop through the saved tracks and create the DB objects, merging into the DB
        # TODO: We could be clever and create unique objects but its easier just to merge for the MVP

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

        logger.info("Saving to Database...")
        bar = Bar("Saving to Database...", max=len(saved_tracks))

        for saved_track_json in saved_tracks.values():
            saved_track = saved_track_from_json(saved_track_json)

            if saved_track.id in audio_features_dict:
                audio_features_json = audio_features_dict[saved_track.id]
                audio_features = audio_features_from_json(audio_features_json)
                saved_track.audio_features = audio_features

            # TODO: https://docs.sqlalchemy.org/en/13/orm/session_state_management.html#merging
            session.add(saved_track)

            bar.next()

        session.commit()

        bar.finish()


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
    except Exception as e:
        logger.exception("Exception occurred:")
        if e is not KeyboardInterrupt:
            raise e
