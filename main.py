from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
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
from spotifysqlite.api import SpotifySession

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


async def main():
    """
    `main` will authorise the user with Spotify and then fetch every track from their
    library. For each track in their library it will add these records to the database:
    - The `SavedTrack`
    - The track's `Genre`s
    - The track's `Artist`s
        - Including the `Artist`'s `Genre`s
    - The track's `Album`
        - This includes the `Artist`s (and their respective `Genre`s) for that `Album`
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

        # logger.info(f"Fetching {total_tracks} tracks...")
        tracks_bar = Bar("Fetching tracks...", max=total_tracks)

        # These all map IDs to the json response for that ID
        saved_tracks: dict[str, dict] = dict()
        albums: dict[str, dict] = dict()
        artists: dict[str, dict] = dict()
        audio_features: dict[str, dict] = dict()

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

                tracks_bar.next()

        tracks_bar.finish()

        # 2. Fetch all the pending albums from step 1 and mark any new artists as pending
        # logger.info(f"Fetching {len(pending_albums)} albums...")

        # 3. Fetch all the pending artists from step 1 and 2
        # logger.info(f"Fetching {len(pending_artists)} artists...")

        # We can also fetch all the audio features in parallel with steps 2 and 3

        # 4. Loop through the saved tracks and create the DB objects, merging into the DB
        # TODO: We could be clever and create unique objects but its easier just to merge for the MVP

        # logger.info("Committing to database...")

        if os.path.exists("test.db"):
            os.remove("test.db")

        engine = db.create_engine("test.db")
        session = Session(engine)

        db_bar = Bar("Committing to database...", max=total_tracks)

        for saved_track_json in saved_tracks.values():
            saved_track = saved_track_from_json(saved_track_json)

            # TODO: https://docs.sqlalchemy.org/en/13/orm/session_state_management.html#merging
            session.add(saved_track)

            db_bar.next()

        session.commit()

        db_bar.finish()


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
    logging.basicConfig(
        format="%(levelname)s [%(asctime)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # logging.getLogger("sqlalchemy.engine").setLevel(logging.INFO)

    try:
        load_dotenv()
        asyncio.run(main())
        logger.info("Finished!")
    except KeyboardInterrupt:
        logger.exception("Keyboard Interrupt!")
