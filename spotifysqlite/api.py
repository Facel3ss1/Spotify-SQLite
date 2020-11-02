from __future__ import annotations

import asyncio
import os
import webbrowser
from math import ceil

# https://docs.python.org/3/library/typing.html
from typing import Any, AsyncGenerator, Callable, Optional, TypedDict, TypeVar
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


# https://realpython.com/async-io-python/#other-features-async-for-and-async-generators-comprehensions
async def batcher(batch_coro, rx_in: MemoryObjectReceiveStream, *, batch_size: int):
    # TODO: a buffer_size parameter
    tx_out, rx_out = create_memory_object_stream()

    async with create_task_group() as tg:
        async with tx_out:
            stream_ended = False

            while not stream_ended:
                batch = list()

                while len(batch) < batch_size:
                    try:
                        item = await rx_in.receive()
                        batch.append(item)
                    except anyio.EndOfStream:
                        stream_ended = True
                        break

                # Each batch corountine will use tx_out and then close it
                await tg.spawn(batch_coro, batch, tx_out.clone())

        # The original tx_out is closed here so rx_out doesn't unblock until
        # all the tx_out clones in the child tasks are closed

        # tx_out.send() blocks until rx_out recieves from it, so if this for loop was
        # outside the task group, the task group would never finish - deadlock!
        async for item in rx_out:
            yield item


async def output_artists(rx_artist_id: MemoryObjectReceiveStream, request_artist_batch):
    artists_unsorted = [
        (a["name"], a["popularity"])
        async for a in batcher(request_artist_batch, rx_artist_id, batch_size=PAGE_SIZE)
    ]

    artists = sorted(artists_unsorted, key=lambda a: a[1], reverse=True)

    for i in range(100):
        (name, popularity) = artists[i]
        print(f"{name} ({popularity})")


async def get_saved_tracks_page(
    spotify: SpotifySession, page: int, tx_track: MemoryObjectSendStream
):
    r = await spotify.get(
        "/v1/me/tracks",
        # https://developer.spotify.com/documentation/general/guides/track-relinking-guide/
        params={
            "limit": PAGE_SIZE,
            # page will start at 0 and go up to total_pages - 1
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

        artist_ids: set[str] = set()

        tx_track, rx_track = create_memory_object_stream()

        async with create_task_group() as tg:
            async with tx_track:
                for page in range(total_pages):
                    await tg.spawn(
                        get_saved_tracks_page, spotify, page, tx_track.clone()
                    )

            async for track in rx_track:
                for artist in track["artists"]:
                    artist_ids.add(artist["id"])

        # TODO: Handle errors from the Spotify API
        async def request_artist_batch(batch, tx_artist: MemoryObjectSendStream):
            r = await spotify.get("/v1/artists", params={"ids": ",".join(batch)})
            json_response = r.json()

            async with tx_artist:
                for artist in json_response["artists"]:
                    await tx_artist.send(artist)

        print(f"Fetching {len(artist_ids)} artists...")

        tx_artist_id, rx_artist_id = create_memory_object_stream()

        async with create_task_group() as tg:
            await tg.spawn(output_artists, rx_artist_id, request_artist_batch)

            async with tx_artist_id:
                for artist_id in artist_ids:
                    await tx_artist_id.send(artist_id)


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
