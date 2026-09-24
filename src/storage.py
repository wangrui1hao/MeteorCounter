import sqlite3
import json
from datetime import datetime
from pathlib import Path


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.directory / 'counts.sqlite3', timeout=5)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS counter (id INTEGER PRIMARY KEY, total INTEGER NOT NULL, active INTEGER NOT NULL);
            INSERT OR IGNORE INTO counter VALUES (1, 0, 0);
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY, time TEXT NOT NULL, action TEXT NOT NULL,
                total INTEGER NOT NULL, score REAL, monitor TEXT);
            CREATE TABLE IF NOT EXISTS bag_sessions (
                id TEXT PRIMARY KEY, started TEXT NOT NULL, meteors INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS bag_snapshots (
                id INTEGER PRIMARY KEY, session TEXT NOT NULL, time TEXT NOT NULL, items TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ball_names (key TEXT PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ball_prices (key TEXT PRIMARY KEY, price TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS round_timing (session TEXT PRIMARY KEY, meteors INTEGER NOT NULL, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS shower_counter (id INTEGER PRIMARY KEY, total INTEGER NOT NULL, active INTEGER NOT NULL);
            INSERT OR IGNORE INTO shower_counter VALUES (1,0,0);
            CREATE TABLE IF NOT EXISTS shower_history (
                id INTEGER PRIMARY KEY, session TEXT NOT NULL, time TEXT NOT NULL, total INTEGER NOT NULL,
                score REAL, monitor TEXT, evidence TEXT);
        ''')
        self.db.commit()
        columns={r[1] for r in self.db.execute('PRAGMA table_info(history)')}
        if 'evidence' not in columns:
            self.db.execute('ALTER TABLE history ADD COLUMN evidence TEXT')
            self.db.commit()
        if 'session' not in columns:self.db.execute('ALTER TABLE history ADD COLUMN session TEXT')
        columns={r[1] for r in self.db.execute('PRAGMA table_info(bag_sessions)')}
        if 'first_meteor_at' not in columns:self.db.execute('ALTER TABLE bag_sessions ADD COLUMN first_meteor_at TEXT')
        if 'showers' not in columns:self.db.execute('ALTER TABLE bag_sessions ADD COLUMN showers INTEGER NOT NULL DEFAULT 0')
        self.db.commit()

    def prices(self):
        return {key:int(price) for key,price in self.db.execute('SELECT key,price FROM ball_prices')}

    def set_price(self, key, price):
        if type(price) is not int or price<0:
            raise ValueError('单价必须是非负整数，0 表示未设置')
        with self.db:
            if price==0:self.db.execute('DELETE FROM ball_prices WHERE key=?',(key,))
            else:self.db.execute('INSERT OR REPLACE INTO ball_prices(key,price) VALUES(?,?)',(key,str(price)))

    def state(self):
        total, active = self.db.execute('SELECT total,active FROM counter WHERE id=1').fetchone()
        return total, bool(active)

    def set_active(self, active):
        with self.db:
            self.db.execute('UPDATE counter SET active=? WHERE id=1', (int(active),))

    def event(self, score, monitor, evidence=None, session=None, when=None):
        now = when or datetime.now().astimezone().isoformat(timespec='seconds')
        with self.db:
            self.db.execute('UPDATE counter SET total=total+1, active=1 WHERE id=1')
            total = self.state()[0]
            self.db.execute('INSERT INTO history(time,action,total,score,monitor,evidence,session) VALUES(?,?,?,?,?,?,?)',
                            (now, '识别到陨星', total, score, monitor,evidence,session))
            if session:self.db.execute('UPDATE bag_sessions SET meteors=?,first_meteor_at=COALESCE(first_meteor_at,?) WHERE id=?',(total,now,session))
        return total, now

    def shower_state(self):
        total,active=self.db.execute('SELECT total,active FROM shower_counter WHERE id=1').fetchone()
        return total,bool(active)

    def set_shower_active(self,active):
        with self.db:self.db.execute('UPDATE shower_counter SET active=? WHERE id=1',(int(active),))

    def shower_event(self,session,score,monitor,evidence=None,when=None):
        now=when or datetime.now().astimezone().isoformat(timespec='seconds')
        with self.db:
            self.db.execute('UPDATE shower_counter SET total=total+1,active=1 WHERE id=1')
            total=self.shower_state()[0]
            self.db.execute('UPDATE bag_sessions SET showers=? WHERE id=?',(total,session))
            self.db.execute('INSERT INTO shower_history(session,time,total,score,monitor,evidence) VALUES(?,?,?,?,?,?)',
                            (session,now,total,score,monitor,evidence))
        return total,now

    def session_showers(self,session):
        return self.db.execute('SELECT showers FROM bag_sessions WHERE id=?',(session,)).fetchone()[0]

    def last_shower(self,session):
        row=self.db.execute('SELECT time FROM shower_history WHERE session=? ORDER BY id DESC LIMIT 1',(session,)).fetchone()
        return row[0] if row else None

    def close(self):
        self.db.close()

    def start_round(self, key):
        now=datetime.now().astimezone().isoformat(timespec='seconds')
        with self.db:
            self.db.execute('INSERT INTO bag_sessions(id,started) VALUES(?,?)',(key,now))
            self.db.execute('UPDATE counter SET total=0 WHERE id=1')
            self.db.execute('INSERT INTO history(time,action,total) VALUES(?,?,0)',(now,'开始新一轮'))
            self.db.execute('UPDATE shower_counter SET total=0 WHERE id=1')

    def latest_round_id(self):
        row=self.db.execute('SELECT id FROM bag_sessions ORDER BY rowid DESC LIMIT 1').fetchone()
        return row[0] if row else None

    def load_latest_round(self, key):
        """Restore the last saved round; never fall back to clearing user data."""
        latest=self.db.execute('SELECT id,started,meteors FROM bag_sessions ORDER BY rowid DESC LIMIT 1').fetchone()
        if not latest or latest[0]!=key or latest[2]!=self.state()[0]:
            raise ValueError('要继续的轮次与最后保存的计数不一致')
        snapshot=self.db.execute('SELECT items FROM bag_snapshots WHERE session=? ORDER BY id DESC LIMIT 1',(key,)).fetchone()
        rows=json.loads(snapshot[0]) if snapshot else {}
        history=self.db.execute("SELECT time,evidence FROM history WHERE action='识别到陨星' AND (session=? OR (session IS NULL AND time>=?)) ORDER BY id DESC LIMIT ?",(key,latest[1],min(5,latest[2]))).fetchall()
        return latest[2],rows,list(reversed(history))

    def round_started(self, key):
        created,first,count,rowid=self.db.execute('SELECT started,first_meteor_at,meteors,rowid FROM bag_sessions WHERE id=?',(key,)).fetchone()
        if first or count==0:return first
        # Older rounds predate the session column; recover their first ordinary
        # meteor without changing the original creation time used for history bounds.
        next_round=self.db.execute('SELECT started FROM bag_sessions WHERE rowid>? ORDER BY rowid LIMIT 1',(rowid,)).fetchone()
        end=next_round[0] if next_round else '9999'
        row=self.db.execute("SELECT time FROM history WHERE action='识别到陨星' AND (session=? OR (session IS NULL AND time>=? AND time<?)) ORDER BY id LIMIT 1",(key,created,end)).fetchone()
        if row:
            with self.db:self.db.execute('UPDATE bag_sessions SET first_meteor_at=? WHERE id=?',(row[0],key))
            return row[0]
        return None

    def save_bag(self, key, rows, meteors):
        with self.db:
            self.db.execute('UPDATE bag_sessions SET meteors=? WHERE id=?',(meteors,key))
            self.db.execute('INSERT INTO bag_snapshots(session,time,items) VALUES(?,?,?)',
                (key,datetime.now().astimezone().isoformat(timespec='seconds'),json.dumps(rows,ensure_ascii=False)))

    def save_timing(self, key, meteors, state):
        with self.db:self.db.execute('INSERT OR REPLACE INTO round_timing VALUES(?,?,?)',
                                    (key,meteors,json.dumps(state)))

    def load_timing(self, key, meteors):
        row=self.db.execute('SELECT state FROM round_timing WHERE session=? AND meteors=?',(key,meteors)).fetchone()
        return json.loads(row[0]) if row else None

    def clear_history(self, current_session):
        if self.latest_round_id()!=current_session:
            raise ValueError('只能保留当前最新轮次并清理历史')
        started=self.db.execute('SELECT started FROM bag_sessions WHERE id=?',(current_session,)).fetchone()[0]
        with self.db:
            for table in ('bag_snapshots','round_timing','shower_history'):
                self.db.execute(f'DELETE FROM {table} WHERE session<>?',(current_session,))
            # Preserve legacy events belonging to the current round, even when
            # their old schema did not yet store an explicit session ID.
            self.db.execute('DELETE FROM history WHERE (session IS NOT NULL AND session<>?) OR (session IS NULL AND time<?)',
                            (current_session,started))
            removed=self.db.execute('DELETE FROM bag_sessions WHERE id<>?',(current_session,)).rowcount
        self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        self.db.execute('VACUUM')
        return removed
