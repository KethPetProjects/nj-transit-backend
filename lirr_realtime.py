"""
LIRR Real-time Status via MTA GTFS-RT
Parses MTA protobuf feed for LIRR train delays/cancellations/track.
Returns status dicts matching NJTransitAPI.get_train_status() format
so the worker can handle NJT and LIRR trains uniformly.

Requires: gtfs-realtime-bindings==1.0.0 (already in requirements.txt)
Env var:  MTA_API_KEY — free key from api.mta.info (optional, but recommended)

Track data uses MTA's MTARR protobuf extension (gtfs_realtime_MTARR_pb2.py).
If that module is unavailable or fails, track is silently omitted.
"""
import os
import requests
from datetime import datetime, timedelta
from typing import Dict, Optional

MTA_LIRR_RT_URL = (
    "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/lirr%2Fgtfs-lirr"
)

PENN_STOP_ID = '237'  # Penn Station stop_id in LIRR GTFS-RT

# Try loading the MTARR extension once at import time
_mtarr_ext = None
try:
    import gtfs_realtime_MTARR_pb2 as _mtarr_mod
    _mtarr_ext = _mtarr_mod.mta_railroad_stop_time_update
    print("✅ LIRR MTARR track extension loaded")
except Exception as _e:
    print(f"⚠️ LIRR MTARR extension unavailable — track disabled: {_e}")

# Per-cycle cache — one fetch per worker cycle (2 min), cleared by clear_cycle_cache()
_rt_data: Optional[Dict] = None   # trip_id → {delay_sec, cancelled}
_rt_fetched: bool = False


def clear_cycle_cache():
    """Clear RT data for this cycle. Call at the start of each worker check cycle."""
    global _rt_data, _rt_fetched
    _rt_data = None
    _rt_fetched = False


def _ensure_rt_data() -> Dict:
    """Fetch GTFS-RT feed once per cycle. Returns trip_id → status map."""
    global _rt_data, _rt_fetched
    if _rt_fetched:
        return _rt_data or {}

    _rt_fetched = True
    api_key = os.getenv('MTA_API_KEY', '')
    headers = {'x-api-key': api_key} if api_key else {}

    try:
        from google.transit import gtfs_realtime_pb2

        resp = requests.get(MTA_LIRR_RT_URL, headers=headers, timeout=10)
        resp.raise_for_status()

        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(resp.content)

        result: Dict[str, dict] = {}
        for entity in feed.entity:
            if not entity.HasField('trip_update'):
                continue
            tu = entity.trip_update
            trip_id = tu.trip.trip_id

            # CANCELED = schedule_relationship 3
            cancelled = (tu.trip.schedule_relationship == 3)

            # Max delay + Penn Station track across all stop_time_updates
            max_delay = 0
            track = ''
            for stu in tu.stop_time_update:
                for field in ('departure', 'arrival'):
                    if stu.HasField(field):
                        delay = getattr(stu, field).delay or 0
                        if delay > max_delay:
                            max_delay = delay

                # Extract track at Penn Station from MTARR extension
                if _mtarr_ext is not None and stu.stop_id == PENN_STOP_ID:
                    try:
                        ext = stu.Extensions[_mtarr_ext]
                        if ext.track:
                            track = ext.track
                    except Exception:
                        pass

            result[trip_id] = {'delay_sec': max_delay, 'cancelled': cancelled, 'track': track}

        _rt_data = result
        print(f"   🚇 LIRR RT: {len(result)} trip update(s) received")
        return result

    except Exception as e:
        print(f"   ⚠️ LIRR RT fetch error: {e}")
        _rt_data = {}
        return {}


def _trip_to_train_number(trip_id: str) -> str:
    """Extract train number from LIRR trip_id.
    Format: GO103_25_905  →  '905'  (last underscore-separated segment)
    """
    return trip_id.split('_')[-1]


def _scheduled_departure(train_number: str) -> Optional[datetime]:
    """Look up today's scheduled departure from LIRR GTFS DB."""
    try:
        import lirr_gtfs
        return lirr_gtfs.get_train_departure_today(train_number)
    except Exception:
        return None


def _train_info(train_number: str) -> dict:
    """Look up line name and headsign for a train number."""
    try:
        import lirr_gtfs
        info = lirr_gtfs.get_train_info(train_number)
        if info:
            return info
    except Exception:
        pass
    return {'line': 'LIRR', 'headsign': '', 'direction_id': None}


def get_train_status(train_number: str) -> Dict:
    """
    Return real-time LIRR train status.

    train_number: trip_short_name / last segment of trip_id — e.g. '905', '1834'

    Return dict (same schema as NJTransitAPI.get_train_status()):
      train_number, scheduled_departure, actual_departure,
      delay_minutes, on_time, delayed, cancelled, status,
      destination, track, line
    """
    data = _ensure_rt_data()

    for trip_id, rt in data.items():
        if _trip_to_train_number(trip_id) != train_number:
            continue

        delay_sec = rt['delay_sec']
        cancelled = rt['cancelled']
        delay_min = delay_sec // 60
        delayed = not cancelled and delay_sec > 300   # >5 min
        on_time = not cancelled and not delayed

        scheduled = _scheduled_departure(train_number)
        actual = (scheduled + timedelta(seconds=delay_sec)) if scheduled and not cancelled else scheduled

        info = _train_info(train_number)
        return {
            'train_number': train_number,
            'scheduled_departure': scheduled,
            'actual_departure': actual,
            'delay_minutes': delay_min,
            'on_time': on_time,
            'delayed': delayed,
            'cancelled': cancelled,
            'status': 'cancelled' if cancelled else ('delayed' if delayed else 'on_time'),
            'destination': info.get('headsign', ''),
            'track': rt.get('track', ''),
            'line': info.get('line', 'LIRR'),
        }

    # Not in RT feed — train has no active delay report; treat as on-time
    scheduled = _scheduled_departure(train_number)
    info = _train_info(train_number)
    return {
        'train_number': train_number,
        'scheduled_departure': scheduled,
        'actual_departure': scheduled,
        'delay_minutes': 0,
        'on_time': True,
        'delayed': False,
        'cancelled': False,
        'status': 'on_time',
        'destination': info.get('headsign', ''),
        'track': '',
        'line': info.get('line', 'LIRR'),
    }
