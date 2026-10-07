#!/usr/bin/env python3
import json
from pathlib import Path
import sys
import time
p=Path('/state')
if len(sys.argv)>1 and sys.argv[1] in ('refresh','reconnect','probe'):
    temp=p/'command.tmp'
    temp.write_text(json.dumps({'action':sys.argv[1],'at':time.time()}))
    temp.replace(p/'command.json')
    print(sys.argv[1].capitalize()+' queued')
else:
    print((p/'status.json').read_text())
