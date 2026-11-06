# -*- coding: utf-8 -*-
"""统一音源接口约定.

每个音源模块提供:
  search(keyword) -> {"songs": [song], "error": str?}
  get_playlist(ref) -> {"songs": [song], "error": str?}   # ref 为源内歌单/收藏夹标识
  get_stream(song) -> {"url": str, "headers": {..}} 或 {"error": str}
  list_my_playlists() -> {"playlists": [{id,name,cover,count}], "error": str?}  # 可选

统一 song 结构:
  {source, id, title, artist, album, duration(秒), cover, extra{..}}
"""


def song(source, id, title, artist="", album="", duration=0, cover="", extra=None):
    return {
        "source": source,
        "id": str(id),
        "title": title or "(未知歌曲)",
        "artist": artist or "",
        "album": album or "",
        "duration": int(duration or 0),
        "cover": cover or "",
        "extra": extra or {},
    }
