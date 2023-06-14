import subprocess
from urllib.parse import urlparse

import ndef

from ntag_mfrc522.ntag215 import NTag215

ntag = NTag215()

ntag.read()
# ntag.print_memory()

records = ntag.get_ndef_records()

for record in records:
    if isinstance(record, ndef.UriRecord):
        uri = urlparse(record.uri)
        context_type, context_id = uri.path.strip("/").split("/")
        print(f"playing back {context_type} {context_id}")
        subprocess.run(
            [
                "spotify_player",
                "playback",
                "start",
                "context",
                "--id",
                context_id,
                context_type,
            ]
        )
