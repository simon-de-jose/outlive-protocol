#!/usr/bin/env python3
"""Quiet cron gate for Hevy sync; advisory locks are released by the kernel."""
from __future__ import annotations
import argparse, contextlib, fcntl, io, json, os, sys, time
from datetime import datetime, timezone
from pathlib import Path
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path: sys.path.insert(0, str(SCRIPT_DIR))
import duckdb
import sync_hevy
STATE_KEY = "coach_strength_cron_state"
LOCK_PATH = Path(os.environ.get("HEVY_SYNC_LOCK", str(sync_hevy.DB_PATH) + ".lock"))

def _now(): return datetime.now(timezone.utc)
def _iso(dt): return dt.astimezone(timezone.utc).isoformat()
def _parse(value):
    if not isinstance(value,str) or not value.strip(): raise ValueError("cursor is missing")
    dt=datetime.fromisoformat(value.strip().replace("Z","+00:00"))
    if dt.tzinfo is None: raise ValueError("cursor must include UTC offset")
    return dt.astimezone(timezone.utc)
class LockBusy(RuntimeError): pass
class FreshnessError(RuntimeError): pass
class RunLock:
    def __init__(self,path): self.path=Path(path); self.fd=None
    def __enter__(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.fd=os.open(self.path,os.O_RDWR|os.O_CREAT,0o600)
        try: fcntl.flock(self.fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except (BlockingIOError,OSError) as exc:
            os.close(self.fd); self.fd=None; raise LockBusy(f"sync lock is held: {self.path}") from exc
        return self
    def __exit__(self,*_):
        if self.fd is not None:
            try: fcntl.flock(self.fd,fcntl.LOCK_UN)
            finally: os.close(self.fd)
def _state(conn):
    row=conn.execute("SELECT value FROM hevy_sync_state WHERE key=?",[STATE_KEY]).fetchone()
    if not row:return None
    value=json.loads(row[0])
    if not isinstance(value,dict):raise ValueError("invalid wrapper state")
    return value
def _save(conn,state): sync_hevy.set_sync_state(conn,STATE_KEY,json.dumps(state,sort_keys=True))
def _classify(exc):
    if isinstance(exc,LockBusy): return "overlap"
    if isinstance(exc,FreshnessError): return "freshness"
    if isinstance(exc,sync_hevy.HevyConfigurationError): return "configuration"
    if isinstance(exc,sync_hevy.HevyTimeoutError) or isinstance(exc,TimeoutError): return "timeout"
    if isinstance(exc,sync_hevy.HevyConnectionError): return "connection"
    if isinstance(exc,duckdb.Error): return "database"
    text=str(exc).lower(); name=type(exc).__name__.lower()
    if "timeout" in name or "deadline" in text or "timed out" in text:return "timeout"
    if "connection" in name or "connection" in text:return "connection"
    if "429" in text or "rate" in text:return "rate_limit"
    return "api" if "http" in text or "request" in name else "unexpected"
def _failure_state(conn,previous,attempted,exc):
    state=dict(previous or {})
    state.setdefault("last_event_time", None)
    state.setdefault("observed_completed_ids", [])
    state.setdefault("ever_observed_completed_ids", state["observed_completed_ids"])
    state.update({"attempted_at":_iso(attempted),"succeeded_at":None,"error_class":_classify(exc),"freshness":False,"newly_observed_completed_workout_ids":[]})
    _save(conn,state)
def run(backfill=False):
    attempted=_now(); conn=None; previous=None
    try:
        with RunLock(LOCK_PATH):
            conn=duckdb.connect(str(sync_hevy.DB_PATH)); previous=_state(conn)
            prior=sync_hevy.get_sync_state(conn,"last_event_time")
            if prior is not None:_parse(prior)
            old_ids=set(previous.get("ever_observed_completed_ids",previous.get("observed_completed_ids",[]))) if previous else set()
            run_start=attempted; deadline=time.monotonic()+sync_hevy._settings()[3]
            conn.execute("BEGIN")
            with contextlib.redirect_stdout(io.StringIO()): sync_hevy.sync_hevy(backfill=backfill,conn=conn,run_start=run_start,deadline=deadline)
            expected_sync=_iso(run_start); expected_cursor=_iso(sync_hevy.cursor_boundary(run_start))
            actual_sync=_parse(sync_hevy.get_sync_state(conn,"last_sync")); actual_cursor=_parse(sync_hevy.get_sync_state(conn,"last_event_time"))
            if actual_sync != _parse(expected_sync) or actual_cursor != _parse(expected_cursor): raise FreshnessError("freshness validation failed")
            current={str(r[0]) for r in conn.execute("SELECT id FROM hevy_workouts WHERE end_time IS NOT NULL").fetchall()}
            ever=old_ids|current; new=sorted(current-old_ids) if previous else []
            state={"attempted_at":expected_sync,"succeeded_at":_iso(_now()),"error_class":None,"freshness":True,"last_event_time":expected_cursor,"newly_observed_completed_workout_ids":new,"ever_observed_completed_ids":sorted(ever),"observed_completed_ids":sorted(current)}
            _save(conn,state); conn.commit()
            if new:
                print("HEVY_SYNC_OK"); print("attempted_at="+state["attempted_at"]); print("succeeded_at="+state["succeeded_at"]); print("freshness=true"); print("newly_observed_completed_workout_ids="+",".join(new))
            else: print("NO_REPLY")
            return 0
    except Exception as exc:
        if conn is not None:
            try: conn.rollback()
            except Exception: pass
            try: conn.execute("BEGIN"); _failure_state(conn,previous,attempted,exc); conn.commit()
            except Exception:
                try: conn.rollback()
                except Exception: pass
        print("HEVY_SYNC_FAILED"); print("attempted_at="+_iso(attempted)); print("error_class="+_classify(exc)); print(f"error={type(exc).__name__}: {exc}")
        return 1
    finally:
        if conn is not None:conn.close()
def main(argv=None):
    parser=argparse.ArgumentParser(description="Reliable Hevy sync wrapper")
    parser.add_argument("--backfill",action="store_true")
    return run(parser.parse_args(argv).backfill)
if __name__ == "__main__": raise SystemExit(main())
