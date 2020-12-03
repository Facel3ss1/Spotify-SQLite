from __future__ import annotations

import asyncio
import logging
import os
import webbrowser
from functools import partial
from math import ceil
from typing import Any, Callable, Coroutine, Optional, TypedDict
from urllib.parse import urljoin

import anyio
import httpx
from anyio import create_memory_object_stream, create_task_group
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from authlib.integrations.httpx_client import AsyncOAuth2Client
from tenacity import RetryCallState, retry
from tenacity.retry import retry_if_exception
from tenacity.stop import stop_after_attempt
from tenacity.wait import wait_random

# https://www.python.org/dev/peps/pep-0589/
# class PagingObject(TypedDict):
#     href: str
#     items: list
#     limit: int
#     offset: int
#     next: Optional[str]
#     previous: Optional[str]
#     total: int

logger = logging.getLogger(__name__)


def _wait_retry_after(retry_state: RetryCallState) -> float:
    http_status_error: httpx.HTTPStatusError = retry_state.outcome.exception()
    r: httpx.Response = http_status_error.response

    if r.status_code == 429:
        retry_after = r.headers["Retry-After"]

        logger.debug(f"Request to {r.url} returned 429. Retry-After: {retry_after}")

        return float(retry_after)
    else:
        return 0


class SpotifySession(AsyncOAuth2Client):
    # https://developer.spotify.com/documentation/general/guides/authorization-guide/#authorization-code-flow
    AUTHORIZATION_BASE_URL = "https://accounts.spotify.com/authorize"
    TOKEN_URL = "https://accounts.spotify.com/api/token"

    API_BASE_URL = "https://api.spotify.com"

    async def authorize_spotify(self, show_dialog: bool = False):
        # TODO: Persist the previous token
        # Authorize with the Spotify API using OAuth2
        # https://docs.authlib.org/en/stable/client/oauth2.html

        # We have to use super() because we override .get() below
        authorization_url, state = super().create_authorization_url(
            self.AUTHORIZATION_BASE_URL,
            # Spotify specific parameter that always shows the web UI for authorizing regardless of prior authorization.
            show_dialog="true" if show_dialog else "false",
        )

        # TODO: This is really annoying please make it stop HTTP server pls
        print(f"Opening {authorization_url} in web browser...")
        webbrowser.open_new(authorization_url)

        authorization_response = input("Enter the full callback URL: ")

        token = await super().fetch_token(
            self.TOKEN_URL, authorization_response=authorization_response
        )

        # print(f"Token: {token}")

    # https://tenacity.readthedocs.io/en/latest/index.html
    # https://developer.spotify.com/documentation/web-api/#rate-limiting
    @retry(
        retry=retry_if_exception(
            lambda e: e.response.status_code == 429 or e.response.status_code == 503
        ),
        stop=stop_after_attempt(5),
        wait=_wait_retry_after + wait_random(0, 2),
    )
    async def get(self, url: str, **kwargs):
        # https://github.com/requests/toolbelt/blob/7c4f92bb81204d82ef01fb0f0ab6dba6c7afc075/requests_toolbelt/sessions.py
        r = await super().get(urljoin(self.API_BASE_URL, url), **kwargs)

        r.raise_for_status()

        return r

    # TODO: Handle errors from the Spotify API

    async def get_multiple_albums(
        self, tx_album: MemoryObjectSendStream, *, ids: list[str]
    ):
        if len(ids) > 50 or len(ids) < 1:
            raise ValueError(
                f"The Get Multiple Albums endpoint only accepts between 1 and 50 IDs! (attempted {len(ids)} IDs)"
            )

        r = await self.get("/v1/albums", params={"ids": ",".join(ids)})
        json_response = r.json()

        async with tx_album:
            for album in json_response["albums"]:
                await tx_album.send(album)

    async def get_multiple_artists(
        self, tx_artist: MemoryObjectSendStream, *, ids: list[str]
    ):
        if len(ids) > 50 or len(ids) < 1:
            raise ValueError(
                f"The Get Multiple Artists endpoint only accepts between 1 and 50 IDs! (attempted {len(ids)} IDs)"
            )

        r = await self.get("/v1/artists", params={"ids": ",".join(ids)})
        json_response = r.json()

        async with tx_artist:
            for artist in json_response["artists"]:
                await tx_artist.send(artist)

    async def get_multiple_audio_features(
        self, tx_audio_features: MemoryObjectSendStream, *, ids: list[str]
    ):
        if len(ids) > 100 or len(ids) < 1:
            raise ValueError(
                f"The Get Multiple Audio Features endpoint only accepts between 1 and 100 IDs! (attempted {len(ids)} IDs)"
            )

        r = await self.get("/v1/audio-features", params={"ids": ",".join(ids)})

        json_response = r.json()
        audio_features_list: list = json_response["audio_features"]

        async with tx_audio_features:
            for audio_features in audio_features_list:
                await tx_audio_features.send(audio_features)

    async def get_followed_artists(
        self, tx_artist: MemoryObjectSendStream, *, limit: int, after: str
    ):
        if limit > 50 or limit < 1:
            raise ValueError(
                f"The Get Followed Artists endpoint only accepts a limit between 1 and 50! (attempted limit={limit})"
            )

        r = await self.get(
            "/v1/me/following",
            params={"type": "artist", "after": after, "limit": limit},
        )

        cursor_paging_object = r.json()
        followed_artists: list = cursor_paging_object["artists"]["items"]

        async with tx_artist:
            for artist in followed_artists:
                await tx_artist.send(artist)

    async def get_saved_albums(
        self, tx_saved_album: MemoryObjectSendStream, *, limit: int, offset: int
    ):
        if limit > 50 or limit < 1:
            raise ValueError(
                f"The Get Saved Albums endpoint only accepts a limit between 1 and 50! (attempted limit={limit})"
            )

        if offset < 0:
            raise ValueError(
                f"The Get Saved Albums endpoint only accepts a positive offset! (attempted offset={offset})"
            )

        r = await self.get(
            "/v1/me/albums",
            params={
                "limit": limit,
                "offset": offset,
                "market": "from_token",
            },
        )

        paging_object = r.json()
        saved_albums: list = paging_object["items"]

        async with tx_saved_album:
            for saved_album in saved_albums:
                await tx_saved_album.send(saved_album)

    async def get_saved_tracks(
        self, tx_saved_track: MemoryObjectSendStream, *, limit: int, offset: int
    ):
        if limit > 50 or limit < 1:
            raise ValueError(
                f"The Get Saved Tracks endpoint only accepts a limit between 1 and 50! (attempted limit={limit})"
            )

        if offset < 0:
            raise ValueError(
                f"The Get Saved Tracks endpoint only accepts a positive offset! (attempted offset={offset})"
            )

        r = await self.get(
            "/v1/me/tracks",
            # https://developer.spotify.com/documentation/general/guides/track-relinking-guide/
            params={
                "limit": limit,
                "offset": offset,
                "market": "from_token",
            },
        )

        paging_object = r.json()
        saved_tracks: list = paging_object["items"]

        async with tx_saved_track:
            for saved_track in saved_tracks:
                await tx_saved_track.send(saved_track)


# TODO: Instead of this being an async generator, we could send the items back using a stream
# https://realpython.com/async-io-python/#other-features-async-for-and-async-generators-comprehensions
async def batcher(
    rx_in: MemoryObjectReceiveStream,
    batch_coro: Callable[[MemoryObjectSendStream, list], Coroutine],
    *,
    batch_size: int,
):
    """
    Takes an input stream, `rx_in`, and splits it up into batches of size `batch_size`,
    then the batches are processed using `batch_coro` and the processed items are yielded.

    For each batch, `batch_coro` is spawned, and it takes the batch and a send stream as
    arguments. `batch_coro` is expected to process the batch, and then send back the
    processed items induvidually using the send stream. These processed items are then
    recieved by the batcher and finally yielded to the caller.
    """
    # TODO: a buffer_size parameter
    tx_item, rx_item = create_memory_object_stream()

    async with create_task_group() as tg:
        async with tx_item:
            stream_ended = False

            while True:
                batch = list()

                while len(batch) < batch_size:
                    try:
                        item = await rx_in.receive()
                        batch.append(item)
                    except anyio.EndOfStream:
                        stream_ended = True
                        break

                if stream_ended:
                    break

                # Each batch corountine will use tx_item and then close it
                await tg.spawn(batch_coro, tx_item.clone(), batch)

        # The original tx_item is closed here so rx_item doesn't unblock until
        # all the tx_item clones in the child tasks are closed

        # tx_item.send() blocks until rx_item recieves from it, so if this for loop was
        # outside the task group, the task group would never finish - deadlock!
        async for item in rx_item:
            yield item


async def main():
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

        logger.info(f"Fetching {total_tracks} tracks...")

        # TODO: When we eventually replace this with a stream, we'll still need to use
        # a set to make sure we aren't requesting the same artist twice.
        artist_ids: set[str] = set()

        tx_saved_track, rx_saved_track = create_memory_object_stream()

        async with create_task_group() as tg:
            async with tx_saved_track:
                for page in range(total_pages):
                    await tg.spawn(
                        partial(
                            spotify.get_saved_tracks,
                            tx_saved_track.clone(),
                            limit=PAGE_SIZE,
                            offset=page * PAGE_SIZE,
                        )
                    )

            async for saved_track in rx_saved_track:
                track = saved_track["track"]
                for artist in track["artists"]:
                    artist_ids.add(artist["id"])

        logger.info(f"Fetching {len(artist_ids)} artists...")

        async def send_artist_ids(
            tx_artist_id: MemoryObjectSendStream, artist_ids: set[str]
        ):
            async with tx_artist_id:
                for artist_id in artist_ids:
                    await tx_artist_id.send(artist_id)

        tx_artist_id, rx_artist_id = create_memory_object_stream()

        async with create_task_group() as tg:
            async with tx_artist_id:
                await tg.spawn(send_artist_ids, tx_artist_id.clone(), artist_ids)

            artists_unsorted = [
                (a["name"], a["popularity"])
                async for a in batcher(
                    rx_artist_id,
                    lambda tx, b: spotify.get_multiple_artists(tx, ids=b),
                    batch_size=PAGE_SIZE,
                )
            ]

            artists = sorted(artists_unsorted, key=lambda a: a[1], reverse=True)

            for i in range(100):
                (name, popularity) = artists[i]
                print(f"{name} ({popularity})")


if __name__ == "__main__":
    # https://github.com/encode/httpx/issues/914
    import sys

    if (
        sys.version_info[0] == 3
        and sys.version_info[1] >= 8
        and sys.platform.startswith("win")
    ):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    from dotenv import load_dotenv

    # https://docs.python.org/3/howto/logging.html
    logging.basicConfig(
        format="%(levelname)s [%(asctime)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    logging.getLogger(__name__).setLevel(logging.DEBUG)

    try:
        load_dotenv()
        asyncio.run(main())
        logger.info("Finished!")
    except KeyboardInterrupt:
        logger.exception("Keyboard Interrupt!")
