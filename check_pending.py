# -*- coding: utf-8 -*-
from db.pending_voices import get_pending, count_pending

print(f"Pending: {count_pending()}")
for p in get_pending():
    print(f"  id={p['id']}, speaker_id={p.get('speaker_id')!r}, "
          f"dur={p.get('duration_sec', 0):.0f}s, "
          f"video={p.get('video_id', '')}")
