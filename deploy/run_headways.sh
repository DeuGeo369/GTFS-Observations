#!/bin/bash
# Always recompute the previous complete service day. The default invocation
# skips days it has already written, which would freeze a partial day's figures
# if it happened to run mid-morning. Yesterday is complete by definition, and
# the upsert makes recomputation idempotent.
D=$(TZ=Australia/Sydney date -d yesterday +%Y%m%d)
echo "computing headways for service date $D"
exec /opt/gtfsobs/.venv/bin/python /opt/gtfsobs/manage.py headways --date "$D"