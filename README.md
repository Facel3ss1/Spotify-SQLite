# Spotify SQLite Database Downloader

This project downloads the metadata of a user's Spotify library using the [Spotify Web API](https://developer.spotify.com/documentation/web-api/).
It stores this metadata in a relational database using [SQLite](https://sqlite.org/index.html).

## Installation

This project requires [Python 3.9+](https://www.python.org/downloads/) to be installed, and you need to install [Pipenv](https://pipenv.pypa.io/en/latest/install/#installing-pipenv) so you can download the dependencies.

Once you've installed those, download the repository and install the dependencies:

```shell
cd <SPOTIFY SQLITE REPOSITORY>
pipenv install
```

Then, you need to create a `.env` file in the root of the repository. Use `.env.example` as a template for this.

In the `.env` file, you need to enter your Client ID and Client Secret from the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard).
It doesn't matter what you use for the Redirect URI (this project doesn't host a server), as long as it's the same as what you entered on the Spotify Dashboard.

## Running

To run the project, use this command:

```shell
pipenv run python main.py
```

This will open your web browser with a prompt asking you to authorise with Spotify.
Once you've accepted, the page will redirect you to the Redirect URI with extra information in the URL.
Note that subsequent runs will redirect you automatically since you've already authorised with Spotify.

Paste the entire URL into the terminal prompt and the program will start to download your Spotify Library.

Once the download has finished, the metadata will be saved to an SQLite database file called `spotifysqlite.db` (this can be changed by passing the filename as a CLI argument), which can be opened in an external program such as [SQLite Studio](https://sqlitestudio.pl/), or on an online viewer such as [SQLite Online](https://sqliteonline.com).

## Building the program

To build this project, install the dev dependencies and then use this command:

```shell
pyinstaller --onefile main.py --name spotifysqlite
```

The output should be in a new `dist` folder.
