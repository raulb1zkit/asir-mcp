#!/usr/bin/env python3
"""Respalda los dos corpus históricos y permite retirarlos tras validar el archivo."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

try:
    from .supabase_db import ROOT, admin_config, connect
except ImportError:
    from supabase_db import ROOT, admin_config, connect

SCHEMAS = ('asir_before_sri', 'asir_previous')
TABLES = ('chunks','citation_segments','citations','corpus','embedding_metadata','embedding_windows','source_occurrences','sources')


def copy_counts(lines):
    counts, table = {}, None
    for line in lines:
        if table is not None:
            if line.rstrip('\n') == r'\.':
                table = None
            else:
                counts[table] += 1
        elif line.startswith('COPY '):
            found = re.match(r'COPY (asir_before_sri|asir_previous)\.([a-z_]+) \(', line)
            if not found:
                raise ValueError('unexpected_dump_table')
            table = found.group(1) + '.' + found.group(2)
            if table in counts:
                raise ValueError('duplicate_copy')
            counts[table] = 0
    if table is not None:
        raise ValueError('incomplete_copy')
    return counts


def snapshot(cursor, schemas):
    from psycopg import sql
    result = {}
    for schema in schemas:
        counts = {}
        for table in TABLES:
            cursor.execute(sql.SQL('SELECT count(*) AS n FROM {}.{}').format(sql.Identifier(schema), sql.Identifier(table)))
            counts[table] = cursor.fetchone()['n']
        cursor.execute(sql.SQL('SELECT corpus_sha256 FROM {}.corpus WHERE id=1').format(sql.Identifier(schema)))
        result[schema] = {'counts': counts, 'corpus_sha256': cursor.fetchone()['corpus_sha256']}
    return result


def check_dependencies(cursor):
    cursor.execute('''SELECT DISTINCT pg_describe_object(d.classid,d.objid,d.objsubid) AS object
      FROM pg_depend d
      CROSS JOIN LATERAL pg_identify_object(d.refclassid,d.refobjid,d.refobjsubid) ref
      CROSS JOIN LATERAL pg_identify_object(d.classid,d.objid,d.objsubid) obj
      WHERE ref.schema=ANY(%s) AND obj.schema IS NOT NULL AND NOT obj.schema=ANY(%s)
      AND NOT (obj.schema='pg_toast' AND d.deptype='i')''', (list(SCHEMAS),list(SCHEMAS)))
    if cursor.fetchall():
        raise ValueError('external_dependencies_review_required')


def run(retire=False):
    from psycopg import sql
    config=admin_config()
    folder=ROOT/'backups'/datetime.now(timezone.utc).strftime('corpus-history-%Y%m%dT%H%M%SZ')
    folder.mkdir(parents=True,mode=0o700)
    archive=folder/'historical.dump'
    # Password exists only in the protected temporary pgpass, never argv/output.
    fd, secret=tempfile.mkstemp(prefix='pgpass-',dir=ROOT/'.private')
    try:
        def escape(value):return str(value).replace('\\','\\\\').replace(':','\\:')
        with os.fdopen(fd,'w') as stream:
            stream.write(':'.join(escape(v) for v in (config['host'],5432,config['database'],config['user'],config['password']))+'\n')
        env={k:v for k,v in os.environ.items() if not k.startswith('PG')}
        env.update(PGPASSFILE=secret,PGSSLMODE='verify-full',PGSSLROOTCERT=config['sslrootcert'],PGCONNECT_TIMEOUT='15')
        with connect(config,application_name='asir-archive-read') as c:
            with c.cursor() as q:
                q.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                q.execute("SET LOCAL statement_timeout='30s'")
                check_dependencies(q)
                served=snapshot(q,('asir',))
                historical=snapshot(q,SCHEMAS)
                q.execute('SELECT pg_export_snapshot() AS id')
                snapshot_id=q.fetchone()['id']
                args=['pg_dump','-w','-h',config['host'],'-p','5432','-U',config['user'],'-d',config['database'],'--format=custom','--no-password','--snapshot='+snapshot_id,'--file='+str(archive)]
                for schema in SCHEMAS:args+=['--schema='+schema]
                result=subprocess.run(args,env=env,capture_output=True,timeout=600)
                if result.returncode:raise ValueError('pg_dump_failed')
            c.rollback()
    finally:
        Path(secret).unlink(missing_ok=True)
    archive.chmod(0o600)
    # Decompress every archive section, then compare COPY row counts to the
    # database snapshot. No educational text is written to terminal or Git.
    result=subprocess.run(['pg_restore','--file=/dev/null',str(archive)],capture_output=True,timeout=300)
    if result.returncode:raise ValueError('archive_stream_invalid')
    with tempfile.TemporaryFile() as errors:
        with subprocess.Popen(['pg_restore','--data-only','--no-owner','--no-privileges','--file=-',str(archive)],stdout=subprocess.PIPE,stderr=errors,text=True) as process:
            actual=copy_counts(process.stdout)
            if process.wait(timeout=300):raise ValueError('archive_data_invalid')
    expected={s+'.'+t:n for s,v in historical.items() for t,n in v['counts'].items()}
    if actual!=expected:raise ValueError('archive_row_counts_mismatch')
    with archive.open('rb') as stream:
        archive_hash=hashlib.file_digest(stream,'sha256').hexdigest()
    report={'archive':str(archive.relative_to(ROOT)),'archive_bytes':archive.stat().st_size,'archive_sha256':archive_hash,'all_sections_readable':True,'table_rows_match_snapshot':True,'restore_rehearsed':False,'historical':historical,'served_before':served,'retired':False}
    report_path=folder/'report.json'
    def save():
        report_path.write_text(json.dumps(report,indent=2)+'\n');report_path.chmod(0o600)
    save()
    if retire:
        with connect(config,application_name='asir-retire-history') as c:
            with c.cursor() as q:
                q.execute("SET LOCAL lock_timeout='5s'")
                q.execute("SET LOCAL statement_timeout='30s'")
                q.execute("SELECT pg_advisory_xact_lock(hashtext('asir-import'))")
                check_dependencies(q)
                # Prevent changes between the recheck and DROP; lock only old tables.
                names=sql.SQL(',').join(sql.SQL('{}.{}').format(sql.Identifier(s),sql.Identifier(t)) for s in SCHEMAS for t in TABLES)
                q.execute(sql.SQL('LOCK TABLE {} IN ACCESS EXCLUSIVE MODE').format(names))
                if snapshot(q,SCHEMAS)!=historical or snapshot(q,('asir',))!=served:raise ValueError('corpus_changed_since_backup')
                for schema in SCHEMAS:q.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
                if snapshot(q,('asir',))!=served:raise ValueError('served_corpus_changed_rollback')
        report['retired']=True
        with connect(config,application_name='asir-retire-verify') as c:
            with c.cursor() as q:
                q.execute('SET TRANSACTION READ ONLY')
                if snapshot(q,('asir',))!=served:raise ValueError('served_corpus_changed_after_commit')
                q.execute('SELECT nspname FROM pg_namespace WHERE nspname=ANY(%s)',(list(SCHEMAS),))
                if q.fetchall():raise ValueError('historical_schema_still_present')
                q.execute('SELECT pg_database_size(current_database()) AS bytes')
                report['database_bytes_after']=q.fetchone()['bytes']
            c.rollback()
        report['served_unchanged']=True
        save()
    print(json.dumps({k:v for k,v in report.items() if k not in ('historical','served_before')},ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--retire',action='store_true',help='Retira solo los dos esquemas históricos tras verificar el respaldo.')
    args=parser.parse_args()
    try:run(args.retire)
    except Exception as e:
        print(json.dumps({'complete':False,'error':str(e) if isinstance(e,ValueError) else type(e).__name__}))
        raise SystemExit(1)
