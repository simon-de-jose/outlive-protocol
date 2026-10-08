from bootstrap.env import db_path
import duckdb

path = db_path()
db = duckdb.connect(str(path), read_only=True)
print(db.sql("SELECT source, COUNT(*) FROM readings GROUP BY source ORDER BY source").fetchall())
db.close()
