SELECT genre.name,
       count(track.id) AS num_tracks,
       round(100.0 * count(track.id) / (
                                           SELECT count(track.id) 
                                             FROM track
                                       ), 2) AS percentage_of_tracks
  FROM track
       INNER JOIN
       track_artist,
       artist_genre,
       genre ON track.id == track_artist.track_id AND 
                track_artist.artist_id == artist_genre.artist_id AND 
                artist_genre.genre_id == genre.id
 WHERE track_artist.artist_order == 0
 GROUP BY genre.id
 ORDER BY num_tracks DESC;

SELECT decade,
       count(decade) 
  FROM (
           SELECT substr(album.release_date, 1, 3) || "0s" AS decade
             FROM track
                  INNER JOIN
                  album ON track.album_id = album.id
       )
 GROUP BY decade;

 SELECT track.name AS track_name,
       artist.name AS artist_name,
       genre.name AS genre_name
  FROM track
       INNER JOIN
       track_artist,
       artist,
       artist_genre,
       genre ON track.id = track_artist.track_id AND 
                track_artist.artist_id = artist.id AND 
                artist.id = artist_genre.artist_id AND 
                artist_genre.genre_id = genre.id
 WHERE track_artist.artist_order = 0 AND 
       genre.name LIKE "%indie%";