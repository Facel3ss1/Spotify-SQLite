from __future__ import annotations

import asyncio
import os
import webbrowser
from math import ceil

# https://docs.python.org/3/library/typing.html
from typing import Any, Callable, Coroutine, Optional, TypedDict
from urllib.parse import urljoin

import anyio
from anyio import create_memory_object_stream, create_task_group
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from authlib.integrations.httpx_client import AsyncOAuth2Client

# https://docs.python.org/3/library/asyncio.html
# https://realpython.com/async-io-python/
# https://www.python-httpx.org/

# TODO: Logging: https://docs.python.org/3/howto/logging.html

# https://developer.spotify.com/documentation/general/guides/authorization-guide/#authorization-code-flow
AUTHORIZATION_BASE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"

# https://developer.spotify.com/documentation/general/guides/scopes/
SCOPE = [
    "user-library-read",
    "user-follow-read",
    # "user-top-read",
    # "user-read-recently-played",
    # "playlist-read-private",
    # "playlist-read-collaborative",
]

SHOW_DIALOG = False

API_BASE_URL = "https://api.spotify.com"

# The 'Get Saved Tracks' endpoint returns pages with a max size of 50 tracks
PAGE_SIZE = 50

# https://www.python.org/dev/peps/pep-0589/
# class PagingObject(TypedDict):
#     href: str
#     items: list
#     limit: int
#     offset: int
#     next: Optional[str]
#     previous: Optional[str]
#     total: int


# TODO: What if there are network problems?
# TODO: https://findwork.dev/blog/advanced-usage-python-requests-timeouts-retries-hooks/
class SpotifySession(AsyncOAuth2Client):
    async def authorize_spotify(self):
        # TODO: Persist the previous token
        # Authorize with the Spotify API using OAuth2
        # https://docs.authlib.org/en/stable/client/oauth2.html

        # We have to use super() because we override .get() below
        authorization_url, state = super().create_authorization_url(
            AUTHORIZATION_BASE_URL,
            # Spotify specific parameter that always shows the web UI for authorizing regardless of prior authorization.
            show_dialog="true" if SHOW_DIALOG else "false",
        )

        # print(f"Opening {authorization_url} in web browser...")
        webbrowser.open_new(authorization_url)

        authorization_response = input("Enter the full callback URL: ")

        token = await super().fetch_token(
            TOKEN_URL, authorization_response=authorization_response
        )

        # print(f"Token: {token}")

    # TODO: https://tenacity.readthedocs.io/en/latest/index.html
    # TODO: https://developer.spotify.com/documentation/web-api/#rate-limiting
    async def get(self, url: str, **kwargs):
        # https://github.com/requests/toolbelt/blob/7c4f92bb81204d82ef01fb0f0ab6dba6c7afc075/requests_toolbelt/sessions.py
        r = await super().get(urljoin(API_BASE_URL, url), **kwargs)

        r.raise_for_status()

        return r


# TODO: Instead of this being an async generator, we could send the items back using a stream
# https://realpython.com/async-io-python/#other-features-async-for-and-async-generators-comprehensions
async def batcher(
    batch_coro: Callable[[list, MemoryObjectSendStream], Coroutine],
    rx_in: MemoryObjectReceiveStream,
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
                await tg.spawn(batch_coro, batch, tx_item.clone())

        # The original tx_item is closed here so rx_item doesn't unblock until
        # all the tx_item clones in the child tasks are closed

        # tx_item.send() blocks until rx_item recieves from it, so if this for loop was
        # outside the task group, the task group would never finish - deadlock!
        async for item in rx_item:
            yield item


# TODO: Handle errors from the Spotify API
async def get_several_artists(
    spotify: SpotifySession, ids: list[str], tx_artist: MemoryObjectSendStream
):
    if len(ids) > 50:
        raise ValueError(
            f"The Get Several Artists endpoint only accepts a maximum of 50 IDs! (attempted {len(ids)} IDs)"
        )

    r = await spotify.get("/v1/artists", params={"ids": ",".join(ids)})
    json_response = r.json()

    async with tx_artist:
        for artist in json_response["artists"]:
            await tx_artist.send(artist)


# TODO: This returns tracks not saved_tracks
async def get_saved_tracks(
    spotify: SpotifySession, page: int, tx_track: MemoryObjectSendStream
):
    r = await spotify.get(
        "/v1/me/tracks",
        # https://developer.spotify.com/documentation/general/guides/track-relinking-guide/
        params={
            "limit": PAGE_SIZE,
            "offset": page * PAGE_SIZE,
            "market": "from_token",
        },
    )

    paging_object = r.json()
    tracks: list = paging_object["items"]

    async with tx_track:
        for item in tracks:
            track = item["track"]
            await tx_track.send(track)


async def main():
    client_id = os.getenv("CLIENT_ID")
    client_secret = os.getenv("CLIENT_SECRET")
    redirect_uri = os.getenv("REDIRECT_URI")

    async with SpotifySession(
        client_id, client_secret, scope=SCOPE, redirect_uri=redirect_uri
    ) as spotify:
        await spotify.authorize_spotify()

        r = await spotify.get(
            "/v1/me/tracks", params={"limit": 1, "market": "from_token"}
        )
        paging_object = r.json()

        # The total no. of tracks in the user's library
        total_tracks = paging_object["total"]
        # How many requests it will take to get all the tracks from the API
        total_pages = ceil(total_tracks / PAGE_SIZE)

        print(f"Fetching {total_tracks} tracks...")

        # TODO: When we eventually replace this with a stream, we'll still need to use
        # a set to make sure we aren't requesting the same artist twice.
        artist_ids: set[str] = set()

        tx_track, rx_track = create_memory_object_stream()

        async with create_task_group() as tg:
            async with tx_track:
                for page in range(total_pages):
                    await tg.spawn(get_saved_tracks, spotify, page, tx_track.clone())

            async for track in rx_track:
                for artist in track["artists"]:
                    artist_ids.add(artist["id"])

        print(f"Fetching {len(artist_ids)} artists...")

        async def send_artist_ids(
            artist_ids: set[str], tx_artist_id: MemoryObjectSendStream
        ):
            async with tx_artist_id:
                for artist_id in artist_ids:
                    await tx_artist_id.send(artist_id)

        tx_artist_id, rx_artist_id = create_memory_object_stream()

        async with create_task_group() as tg:
            async with tx_artist_id:
                await tg.spawn(send_artist_ids, artist_ids, tx_artist_id.clone())

            artists_unsorted = [
                (a["name"], a["popularity"])
                async for a in batcher(
                    lambda b, tx: get_several_artists(spotify, b, tx),
                    rx_artist_id,
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

    try:
        load_dotenv()
        asyncio.run(main())
        print("Finished!")
    except KeyboardInterrupt:
        print("Keyboard Interrupt!")
