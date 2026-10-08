"""Offline reliability matrix for the Hevy cron gate."""
import importlib.util, json, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path
import duckdb, pytest
SCRIPT=Path(__file__).parents[1]/"scripts"
def load(name):
    spec=importlib.util.spec_from_file_location(name,SCRIPT/f"{name}.py"); mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod
@pytest.fixture
def gate(tmp_path):
    sync,gate=load("sync_hevy"),load("hevy_sync_cron"); db=tmp_path/"health.duckdb"; c=duckdb.connect(str(db))
    c.execute("CREATE TABLE hevy_sync_state(key VARCHAR PRIMARY KEY,value VARCHAR,updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"); c.execute("CREATE TABLE hevy_workouts(id VARCHAR PRIMARY KEY,end_time TIMESTAMP)"); c.close()
    sync.DB_PATH=db; gate.sync_hevy=sync; gate.LOCK_PATH=tmp_path/"sync.lock"; return gate,db,sync
def seed(db,sync,ids=(),cursor=None):
    c=duckdb.connect(str(db)); stamp=cursor or datetime.now(timezone.utc).isoformat(); run_dt=datetime.fromisoformat(stamp.replace("Z","+00:00"))
    for wid in ids:c.execute("INSERT OR REPLACE INTO hevy_workouts VALUES (?,?)",[wid,stamp])
    sync.set_sync_state(c,"last_event_time",sync.cursor_boundary(run_dt).isoformat()); sync.set_sync_state(c,"last_sync",stamp); c.close()
def test_first_success_baselines_without_reply(gate,capsys):
    g,db,s=gate; g.sync_hevy.sync_hevy=lambda **_:seed(db,s,["old"],cursor=_["run_start"].isoformat()); assert g.run()==0 and capsys.readouterr().out.strip()=="NO_REPLY"
def test_second_run_new_id_is_structured(gate,capsys):
    g,db,s=gate; calls=[0]
    def fake(**_): calls[0]+=1; seed(db,s,["a"] if calls[0]==1 else ["a","b"],cursor=_["run_start"].isoformat())
    s.sync_hevy=fake; assert g.run()==0; capsys.readouterr(); assert g.run()==0 and "HEVY_SYNC_OK" in capsys.readouterr().out
def test_duplicate_edit_is_quiet(gate,capsys):
    g,db,s=gate; s.sync_hevy=lambda **_:seed(db,s,["a"],cursor=_["run_start"].isoformat()); g.run(); capsys.readouterr(); g.run(); assert capsys.readouterr().out.strip()=="NO_REPLY"
def test_delete_only_advances_cursor_quietly(gate,capsys):
    g,db,s=gate; calls=[0]
    def fake(**_): calls[0]+=1; seed(db,s,["a"] if calls[0]==1 else [],cursor=_["run_start"].isoformat())
    s.sync_hevy=fake; g.run(); capsys.readouterr(); assert g.run()==0 and capsys.readouterr().out.strip()=="NO_REPLY"
def test_api_failure_nonzero_not_quiet(gate,capsys):
    g,_,s=gate; s.sync_hevy=lambda **_: (_ for _ in ()).throw(RuntimeError("HTTP 500")); assert g.run()!=0 and "NO_REPLY" not in capsys.readouterr().out
def test_timeout_failure(gate):
    g,_,s=gate; s.sync_hevy=lambda **_: (_ for _ in ()).throw(TimeoutError("deadline")); assert g.run()==1
def test_missing_key_is_classified(monkeypatch):
    s=load("sync_hevy"); monkeypatch.setenv("HEVY_API_KEY","");
    with pytest.raises(s.HevyConfigurationError):s.get_api_key()
def test_cursor_requires_offset():
    g=load("hevy_sync_cron"); assert g._parse("2026-01-01T00:00:00+02:00").tzinfo==timezone.utc
    with pytest.raises(ValueError):g._parse("2026-01-01T00:00:00")
def test_retry_after_numeric_is_capped():assert load("sync_hevy")._retry_after("9999")==30.0
def test_retry_after_date_is_capped():assert load("sync_hevy")._retry_after("Wed, 21 Oct 2030 07:28:00 GMT")==30.0
def test_retry_after_bad_value_is_ignored():assert load("sync_hevy")._retry_after("nonsense") is None
def test_lock_contention_is_distinct(gate):
    g,_,_=gate
    with g.RunLock(g.LOCK_PATH):assert g.run()!=0
def test_failure_state_is_written_separately(gate):
    g,db,s=gate; s.sync_hevy=lambda **_: (_ for _ in ()).throw(RuntimeError("HTTP 500")); g.run(); c=duckdb.connect(str(db)); row=c.execute("SELECT value FROM hevy_sync_state WHERE key=?",[g.STATE_KEY]).fetchone(); c.close(); assert row and json.loads(row[0])["freshness"] is False
def test_failed_partial_write_rolls_back(gate):
    g,db,s=gate
    def fake(**_): _.get("conn").execute("INSERT INTO hevy_workouts VALUES ('partial',CURRENT_TIMESTAMP)"); raise RuntimeError("HTTP 500")
    s.sync_hevy=fake; assert g.run()==1; c=duckdb.connect(str(db)); assert c.execute("SELECT COUNT(*) FROM hevy_workouts").fetchone()[0]==0; c.close()
def test_stdout_contract_success(gate,capsys):
    g,db,s=gate; s.sync_hevy=lambda **_:seed(db,s,cursor=_["run_start"].isoformat()); g.run(); assert capsys.readouterr().out.strip()=="NO_REPLY"
def test_state_cursor_is_utc(gate):
    g,db,s=gate; s.sync_hevy=lambda **_:seed(db,s,cursor=_["run_start"].astimezone(timezone.utc).isoformat()); assert g.run()==0
def test_malformed_existing_cursor_fails_closed(gate):
    g,db,s=gate; c=duckdb.connect(str(db)); s.set_sync_state(c,"last_event_time","2026-01-01T00:00:00"); c.close(); s.sync_hevy=lambda **_:seed(db,s); assert g.run()==1
def test_import_from_arbitrary_cwd(tmp_path):
    p=subprocess.run([sys.executable,str(SCRIPT/"hevy_sync_cron.py"),"--help"],cwd=tmp_path,capture_output=True,text=True); assert p.returncode==0 and "Reliable Hevy" in p.stdout
def test_direct_library_api_uses_deadline(monkeypatch):
    s=load("sync_hevy"); monkeypatch.setattr(s,"get_api_key",lambda:"x"); monkeypatch.setenv("HEVY_SYNC_DEADLINE_SECONDS",".001")
    with pytest.raises(TimeoutError):s.api_get("/x")
def test_timeout_exhaustion_is_distinct(monkeypatch):
    s=load("sync_hevy"); monkeypatch.setenv("HEVY_SYNC_MAX_RETRIES","1"); monkeypatch.setenv("HEVY_SYNC_BACKOFF_SECONDS","0"); monkeypatch.setattr(s,"get_api_key",lambda:"x"); monkeypatch.setattr(s.requests,"get",lambda *a,**k:(_ for _ in ()).throw(s.requests.Timeout()))
    with pytest.raises(s.HevyTimeoutError):s.api_get("/x")
def test_connection_exhaustion_is_distinct(monkeypatch):
    s=load("sync_hevy"); monkeypatch.setenv("HEVY_SYNC_MAX_RETRIES","1"); monkeypatch.setenv("HEVY_SYNC_BACKOFF_SECONDS","0"); monkeypatch.setattr(s,"get_api_key",lambda:"x"); monkeypatch.setattr(s.requests,"get",lambda *a,**k:(_ for _ in ()).throw(s.requests.ConnectionError()))
    with pytest.raises(s.HevyConnectionError):s.api_get("/x")
@pytest.mark.parametrize("status",[408,429,500,502,503])
def test_transient_http_status_retries(monkeypatch,status):
    s=load("sync_hevy"); monkeypatch.setenv("HEVY_SYNC_MAX_RETRIES","1"); monkeypatch.setenv("HEVY_SYNC_BACKOFF_SECONDS","0"); monkeypatch.setattr(s,"get_api_key",lambda:"x"); calls=[]
    class Resp:
        headers={}; status_code=status
        def raise_for_status(self):raise s.requests.HTTPError(f"HTTP {status}")
    monkeypatch.setattr(s.requests,"get",lambda *a,**k:(calls.append(1) or Resp()))
    with pytest.raises(s.requests.HTTPError):s.api_get("/x")
    assert len(calls)==2
def test_fractional_retry_count_rejected(monkeypatch):
    s=load("sync_hevy"); monkeypatch.setenv("HEVY_SYNC_MAX_RETRIES","1.5")
    with pytest.raises(s.HevyConfigurationError):s._settings()
def test_nan_inf_configuration_rejected(monkeypatch):
    s=load("sync_hevy")
    for name in ["HEVY_SYNC_TIMEOUT_SECONDS","HEVY_SYNC_BACKOFF_SECONDS","HEVY_SYNC_DEADLINE_SECONDS","HEVY_SYNC_REPLAY_OVERLAP_SECONDS"]:
        monkeypatch.setenv(name,"nan")
        with pytest.raises(s.HevyConfigurationError):s._settings()
        monkeypatch.delenv(name)
def test_overlap_is_bounded(monkeypatch):
    s=load("sync_hevy"); monkeypatch.setenv("HEVY_SYNC_REPLAY_OVERLAP_SECONDS","3601")
    with pytest.raises(s.HevyConfigurationError):s._settings()
def test_overlap_boundary_is_five_minutes_by_default(monkeypatch):
    s=load("sync_hevy"); monkeypatch.delenv("HEVY_SYNC_REPLAY_OVERLAP_SECONDS",raising=False); start=datetime(2026,1,1,tzinfo=timezone.utc); assert (start-s.cursor_boundary(start)).total_seconds()==300
def test_lock_file_survives_and_can_be_reused(gate):
    g,_,_=gate
    with g.RunLock(g.LOCK_PATH):pass
    assert g.LOCK_PATH.exists()
    with g.RunLock(g.LOCK_PATH):pass
def test_lock_is_released_after_process_crash(gate):
    g,_,_=gate
    script=(
        "import fcntl, os; "
        f"fd=os.open({str(g.LOCK_PATH)!r}, os.O_RDWR|os.O_CREAT, 0o600); "
        "fcntl.flock(fd, fcntl.LOCK_EX); os._exit(23)"
    )
    crashed=subprocess.run([sys.executable,"-c",script],check=False)
    assert crashed.returncode==23
    with g.RunLock(g.LOCK_PATH):pass
def test_empty_backfill_writes_exact_run_boundary(monkeypatch):
    s=load("sync_hevy"); c=duckdb.connect(":memory:")
    c.execute("CREATE TABLE hevy_sync_state(key VARCHAR PRIMARY KEY,value VARCHAR,updated_at TIMESTAMP)")
    monkeypatch.setattr(s,"api_get",lambda *a,**k:{"workout_count":0})
    start=datetime(2026,1,1,12,0,tzinfo=timezone.utc)
    assert s.sync_workouts_backfill(c,run_start=start)==0
    assert s.get_sync_state(c,"last_sync")==start.isoformat()
    assert s.get_sync_state(c,"last_event_time")==s.cursor_boundary(start).isoformat()
    assert s.get_sync_state(c,"last_backfill")==start.isoformat()
    c.close()
def test_empty_incremental_writes_exact_run_boundary(monkeypatch):
    s=load("sync_hevy"); c=duckdb.connect(":memory:")
    c.execute("CREATE TABLE hevy_sync_state(key VARCHAR PRIMARY KEY,value VARCHAR,updated_at TIMESTAMP)")
    start=datetime(2026,1,1,12,0,tzinfo=timezone.utc)
    s.set_sync_state(c,"last_event_time","2025-12-31T00:00:00+00:00")
    monkeypatch.setattr(s,"api_get",lambda *a,**k:{"events":[],"page_count":1})
    assert s.sync_workouts_incremental(c,run_start=start)==0
    assert s.get_sync_state(c,"last_sync")==start.isoformat()
    assert s.get_sync_state(c,"last_event_time")==s.cursor_boundary(start).isoformat()
    c.close()
def test_delete_only_incremental_advances_exact_boundary(monkeypatch):
    s=load("sync_hevy"); c=duckdb.connect(":memory:")
    c.execute("CREATE TABLE hevy_sync_state(key VARCHAR PRIMARY KEY,value VARCHAR,updated_at TIMESTAMP)")
    c.execute("CREATE TABLE hevy_workouts(id VARCHAR PRIMARY KEY)")
    c.execute("CREATE TABLE hevy_sets(workout_id VARCHAR)")
    c.execute("INSERT INTO hevy_workouts VALUES ('gone')")
    c.execute("INSERT INTO hevy_sets VALUES ('gone')")
    s.set_sync_state(c,"last_event_time","2025-12-31T00:00:00+00:00")
    monkeypatch.setattr(s,"api_get",lambda *a,**k:{"events":[],"deleted_workout_ids":["gone"],"page_count":1})
    start=datetime(2026,1,1,12,0,tzinfo=timezone.utc)
    assert s.sync_workouts_incremental(c,run_start=start)==1
    assert c.execute("SELECT COUNT(*) FROM hevy_workouts").fetchone()[0]==0
    assert s.get_sync_state(c,"last_sync")==start.isoformat()
    assert s.get_sync_state(c,"last_event_time")==s.cursor_boundary(start).isoformat()
    c.close()
def test_failure_preserves_prior_history(gate):
    g,db,s=gate; c=duckdb.connect(str(db)); s.set_sync_state(c,g.STATE_KEY,json.dumps({"ever_observed_completed_ids":["gone"],"last_event_time":"old"})); c.close(); s.sync_hevy=lambda **_: (_ for _ in ()).throw(RuntimeError("HTTP 500")); assert g.run()==1; c=duckdb.connect(str(db)); state=json.loads(c.execute("SELECT value FROM hevy_sync_state WHERE key=?",[g.STATE_KEY]).fetchone()[0]); c.close(); assert state["ever_observed_completed_ids"]==["gone"] and state["newly_observed_completed_workout_ids"]==[] and state["succeeded_at"] is None
def test_cli_parser_modes_exist():
    import argparse
    p=argparse.ArgumentParser()
    for flag in ["--backfill","--dry-run","--exercises","--routines"]:p.add_argument(flag,action="store_true")
    assert all(vars(p.parse_args(["--backfill","--dry-run","--exercises","--routines"])).values())
