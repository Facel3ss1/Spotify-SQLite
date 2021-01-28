from __future__ import annotations

import logging
import webbrowser
from urllib.parse import urljoin

import httpx
from anyio.streams.memory import MemoryObjectSendStream
from authlib.integrations.httpx_client import AsyncOAuth2Client
from tenacity import RetryCallState, retry
from tenacity.retry import retry_if_exception
from tenacity.stop import stop_after_attempt
from tenacity.wait import wait_random

logger = logging.getLogger(__name__)


# We use this in SpotifySession.get() in the @retry decorator
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

    async def authorize_spotify(
        self, *, show_dialog: bool = False, open_browser: bool = True
    ):
        # Authorize with the Spotify API using OAuth2
        # https://docs.authlib.org/en/stable/client/oauth2.html

        # We have to use super() because we override .get() below
        authorization_url, state = super().create_authorization_url(
            self.AUTHORIZATION_BASE_URL,
            # Spotify specific parameter that always shows the web UI for authorizing regardless of prior authorization.
            show_dialog="true" if show_dialog else "false",
        )

        # TODO: This is really annoying please make it stop HTTP server pls
        # Or we could save the previous token
        print(f"Opening {authorization_url} in web browser...")

        if open_browser:
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
        wait=_wait_retry_after + wait_random(0, 1),
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
        if len(ids) > 20 or len(ids) < 1:
            raise ValueError(
                f"The Get Multiple Albums endpoint only accepts between 1 and 20 IDs! (attempted {len(ids)} IDs)"
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

    async def get_followed_artists(self, *, limit: int, after: str) -> list[dict]:
        if limit > 50 or limit < 1:
            raise ValueError(
                f"The Get Followed Artists endpoint only accepts a limit between 1 and 50! (attempted limit={limit})"
            )

        params = {"type": "artist", "limit": limit}

        if after is not None:
            params["after"] = after

        r = await self.get("/v1/me/following", params=params)

        cursor_paging_object = r.json()
        followed_artists: list[dict] = cursor_paging_object["artists"]["items"]

        return followed_artists

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
