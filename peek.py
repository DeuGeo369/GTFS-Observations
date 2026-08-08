from datetime import datetime, timezone
from google.transit import gtfs_realtime_pb2

feed = gtfs_realtime_pb2.FeedMessage()
with open("feed.pb", "rb") as f:
    feed.ParseFromString(f.read())

ts = feed.header.timestamp
print("feed timestamp :", datetime.fromtimestamp(ts, timezone.utc).astimezone())
print("entities       :", len(feed.entity))

# Look at the first trip that actually has stop predictions
for entity in feed.entity:
    tu = entity.trip_update
    if not tu.stop_time_update:
        continue

    print("\ntrip_id  :", tu.trip.trip_id)
    print("route_id :", tu.trip.route_id)
    print("date     :", tu.trip.start_date)
    print("stops predicted:", len(tu.stop_time_update))

    for stu in tu.stop_time_update[:5]:
        when = stu.arrival.time or stu.departure.time
        clock = datetime.fromtimestamp(when, timezone.utc).astimezone().strftime("%H:%M:%S") if when else "-"
        print(f"  seq {stu.stop_sequence:>3}  stop {stu.stop_id:<10} "
              f"arr {clock}  delay {stu.arrival.delay:>5}s")
    break
