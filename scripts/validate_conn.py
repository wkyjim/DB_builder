import sys, os
sys.path.insert(0, r"C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder\src")

from db_builder.config import local_database_url, neon_database_url, local_engine, neon_engine

print("LOCAL_DATABASE_URL=CONSTRUCTED")
print("NEON_DATABASE_URL=CONSTRUCTED")

try:
    engine = local_engine()
    with engine.connect() as conn:
        result = conn.execute(sys.modules['sqlalchemy'].text("SELECT 1 as val"))
        row = result.fetchone()
        print("LOCAL_DB_CONNECTIVITY=OK (SELECT 1={})".format(row[0] if row else "?"))
    print("LOCAL_DB_OK=TRUE")
except Exception as e:
    print("LOCAL_DB_CONNECTIVITY=FAILED: " + str(e)[:200])
    print("LOCAL_DB_OK=FALSE")

try:
    engine = neon_engine()
    with engine.connect() as conn:
        result = conn.execute(sys.modules['sqlalchemy'].text("SELECT 1 as val"))
        row = result.fetchone()
        print("NEON_DB_CONNECTIVITY=OK (SELECT 1={})".format(row[0] if row else "?"))
    print("NEON_DB_OK=TRUE")
except Exception as e:
    print("NEON_DB_CONNECTIVITY=FAILED: " + str(e)[:200])
    print("NEON_DB_OK=FALSE")

print("VALIDATION_DONE")
