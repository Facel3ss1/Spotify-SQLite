from __future__ import annotations

import asyncio
import datetime
import os
import webbrowser

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

# https://www.python.org/dev/peps/pep-0589/
class PagingObject(TypedDict):
    href: str
    items: list
    limit: int
    offset: int
    next: Optional[str]
    previous: Optional[str]
    total: int


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
        webbrowser.open(authorization_url)

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


# https://mypy.readthedocs.io/en/stable/generics.html
T = TypeVar("T")

# TODO: Can we do this asynchronously?
# https://realpython.com/async-io-python/#other-features-async-for-and-async-generators-comprehensions
async def batcher(
    fn: Callable[[set[str]], AsyncGenerator[T, None]],
    ids: set[str],
    *,
    batch_size: int = 50,
) -> AsyncGenerator[T, None]:
    while len(ids) > 0:
        batch: set[str] = set()

        while len(batch) < batch_size:
            try:
                batch.add(ids.pop())
            except KeyError:
                break

        async for item in fn(batch):
            yield item


async def saved_tracks():
    client_id = os.getenv("CLIENT_ID")
    client_secret = os.getenv("CLIENT_SECRET")
    redirect_uri = os.getenv("REDIRECT_URI")

    async with SpotifySession(
        client_id, client_secret, scope=SCOPE, redirect_uri=redirect_uri
    ) as spotify:
        await spotify.authorize_spotify()

        artist_ids = set()

        # TODO: Use "next" on the paging object and a while loop
        for page in range(10):
            PAGE_SIZE = 50

            r = await spotify.get(
                "/v1/me/tracks",
                # https://developer.spotify.com/documentation/general/guides/track-relinking-guide/
                params={
                    "limit": PAGE_SIZE,
                    "offset": page * PAGE_SIZE,
                    "market": "from_token",
                },
            )

            paging_object: PagingObject = r.json()
            tracks = paging_object["items"]

            for item in tracks:
                track = item["track"]

                for artist in track["artists"]:
                    artist_ids.add(artist["id"])

        # TODO: Handle errors from the Spotify API
        async def request_artist_batch(batch: set):
            r = await spotify.get("/v1/artists", params={"ids": ",".join(batch)})

            json_response = r.json()

            for artist in json_response["artists"]:
                yield artist

        artists_unsorted = [
            (a["name"], a["popularity"])
            async for a in batcher(request_artist_batch, artist_ids)
        ]

        artists = sorted(artists_unsorted, key=lambda a: a[1], reverse=True)

        for (name, popularity) in artists:
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
