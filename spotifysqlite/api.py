from __future__ import annotations

import asyncio
import os
import webbrowser

from anyio import create_task_group, create_memory_object_stream
from math import ceil

# https://docs.python.org/3/library/typing.html
from typing import Any, AsyncGenerator, Callable, Optional, TypedDict, TypeVar
from urllib.parse import urljoin

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
    # https://developer.spotify.com/documentation/web-api/#rate-limiting
    async def get(self, url: str, **kwargs):
        # https://github.com/requests/toolbelt/blob/7c4f92bb81204d82ef01fb0f0ab6dba6c7afc075/requests_toolbelt/sessions.py
        r = await super().get(urljoin(API_BASE_URL, url), **kwargs)

        r.raise_for_status()

        return r


# https://realpython.com/async-io-python/#other-features-async-for-and-async-generators-comprehensions
# TODO: Make ids into rx_id
async def batcher(fn, ids: set[str], *, batch_size: int):
    tx_item, rx_item = create_memory_object_stream()

    async with create_task_group() as tg:
        async with tx_item:
            while len(ids) > 0:
                batch: list[str] = list()

                while len(batch) < batch_size:
                    try:
                        batch.append(ids.pop())
                    except KeyError:
                        break

                # Each batch function will use tx_item and then close it
                await tg.spawn(fn, batch, tx_item.clone())

        # The original tx_item is closed here so rx_item doesn't block despite
        # all the tx_item clones in the child tasks being closed

        # tx_item.send() blocks until rx_item recieves it, so if this for loop was
        # outside the task group, the task group would never finish - deadlock!
        async for item in rx_item:
            yield item


async def saved_tracks():
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

        for page in range(total_pages):
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

            for item in tracks:
                track = item["track"]

                for artist in track["artists"]:
                    artist_ids.add(artist["id"])

        # TODO: Handle errors from the Spotify API
        async def request_artist_batch(batch, tx_artist):
            r = await spotify.get("/v1/artists", params={"ids": ",".join(batch)})

            json_response = r.json()

            async with tx_artist:
                for artist in json_response["artists"]:
                    await tx_artist.send(artist)

        print(f"Fetching {len(artist_ids)} artists...")

        artists_unsorted = [
            (a["name"], a["popularity"])
            async for a in batcher(
                request_artist_batch, artist_ids, batch_size=PAGE_SIZE
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
        asyncio.run(saved_tracks())
        print("Finished!")
    except KeyboardInterrupt:
        print("Keyboard Interrupt!")
