import os

import pytest
from anyio import create_memory_object_stream
from dotenv import load_dotenv

from spotifysqlite import api

pytestmark = pytest.mark.anyio


@pytest.fixture
def env_vars():
    load_dotenv()

    CLIENT_ID = os.getenv("CLIENT_ID")
    CLIENT_SECRET = os.getenv("CLIENT_SECRET")
    REDIRECT_URI = os.getenv("REDIRECT_URI")

    if CLIENT_ID is None:
        raise Exception("CLIENT_ID not found in environment")

    if CLIENT_SECRET is None:
        raise Exception("CLIENT_SECRET not found in environment")

    if REDIRECT_URI is None:
        raise Exception("REDIRECT_URI not found in environment")

    return {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
    }


@pytest.fixture
async def spotify_session(env_vars):
    SCOPE = ["user-library-read", "user-follow-read"]

    spotify = api.SpotifySession(scope=SCOPE, **env_vars)
    await spotify.authorize_spotify()

    yield spotify

    await spotify.aclose()


class TestSpotifySession:
    async def test_get_saved_albums(_, spotify_session: api.SpotifySession):
        tx_saved_album, _ = create_memory_object_stream()

        async with tx_saved_album:
            with pytest.raises(ValueError):
                await spotify_session.get_saved_albums(
                    tx_saved_album.clone(), limit=100, offset=0
                )
