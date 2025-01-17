import datetime
from aiofiles import os as aios
import os
import time

from ..tl import types
from .memory import MemorySession, _SentFileType
from .. import utils
from ..crypto import AuthKey
from ..tl.types import (
    InputPhoto, InputDocument, PeerUser, PeerChat, PeerChannel
)

try:
    import aiosqlite

    aiosqlite_err = None
except ImportError as e:
    aiosqlite = None
    aiosqlite_err = type(e)

EXTENSION = '.session'
CURRENT_VERSION = 7  # database version


class SQLiteSession(MemorySession):
    """This session contains the required information to login into your
       Telegram account. NEVER give the saved session file to anyone, since
       they would gain instant access to all your messages and contacts.

       If you think the session has been compromised, close all the sessions
       through an official Telegram client to revoke the authorization.
    """

    def __init__(self, session_id=None):
        super().__init__()
        self._conn = None
        self.filename = None
        self.save_entities = None
        self.session_id = session_id

    async def async_init(self):
        if aiosqlite is None:
            raise aiosqlite_err

        self.filename = ':memory:'
        self.save_entities = True

        if self.session_id:
            self.filename = self.session_id
            if not self.filename.endswith(EXTENSION):
                self.filename += EXTENSION

        self._conn = None
        c = await self._cursor()
        await c.execute("select name from sqlite_master "
                        "where type='table' and name='version'")
        if await c.fetchone():
            # Tables already exist, check for the version
            await c.execute("select version from version")
            version = (await c.fetchone())[0]
            if version < CURRENT_VERSION:
                await self._upgrade_database(old=version)
                await c.execute("delete from version")
                await c.execute("insert into version values (?)", (CURRENT_VERSION,))
                await self.save()

            # These values will be saved
            await c.execute('select * from sessions')
            tuple_ = await c.fetchone()
            if tuple_:
                self._dc_id, self._server_address, self._port, key, \
                    self._takeout_id = tuple_
                self._auth_key = AuthKey(data=key)

            await c.close()
        else:
            # Tables don't exist, create new ones
            await self._create_table(
                c,
                "version (version integer primary key)",
                """sessions (
                    dc_id integer primary key,
                    server_address text,
                    port integer,
                    auth_key blob,
                    takeout_id integer
                )""",
                """entities (
                    id integer primary key,
                    hash integer not null,
                    username text,
                    phone integer,
                    name text,
                    date integer
                )""",
                """sent_files (
                    md5_digest blob,
                    file_size integer,
                    type integer,
                    id integer,
                    hash integer,
                    primary key(md5_digest, file_size, type)
                )""",
                """update_state (
                    id integer primary key,
                    pts integer,
                    qts integer,
                    date integer,
                    seq integer
                )"""
            )
            await c.execute("insert into version values (?)", (CURRENT_VERSION,))
            await self._update_session_table()
            await c.close()
            await self.save()

    async def clone(self, to_instance=None):
        cloned = super().clone(to_instance)
        cloned.save_entities = self.save_entities
        return cloned

    async def _upgrade_database(self, old):
        c = await self._cursor()
        if old == 1:
            old += 1
            # old == 1 doesn't have the old sent_files so no need to drop
        if old == 2:
            old += 1
            # Old cache from old sent_files lasts then a day anyway, drop
            await c.execute('drop table sent_files')
            await self._create_table(c, """sent_files (
                md5_digest blob,
                file_size integer,
                type integer,
                id integer,
                hash integer,
                primary key(md5_digest, file_size, type)
            )""")
        if old == 3:
            old += 1
            await self._create_table(c, """update_state (
                id integer primary key,
                pts integer,
                qts integer,
                date integer,
                seq integer
            )""")
        if old == 4:
            old += 1
            await c.execute("alter table sessions add column takeout_id integer")
        if old == 5:
            # Not really any schema upgrade, but potentially all access
            # hashes for User and Channel are wrong, so drop them off.
            old += 1
            await c.execute('delete from entities')
        if old == 6:
            old += 1
            await c.execute("alter table entities add column date integer")

        await c.close()

    @staticmethod
    async def _create_table(c, *definitions):
        for definition in definitions:
            await c.execute('create table {}'.format(definition))

    # Data from sessions should be kept as properties
    # not to fetch the database every time we need it
    async def set_dc(self, dc_id, server_address, port):
        super().set_dc(dc_id, server_address, port)
        await self._update_session_table()

        # Fetch the auth_key corresponding to this data center
        row = await self._execute('select auth_key from sessions')
        if row and row[0]:
            self._auth_key = AuthKey(data=row[0])
        else:
            self._auth_key = None

    async def set_auth_key(
            self,
            value
    ):
        self._auth_key = value
        await self._update_session_table()

    async def set_takeout_id(
            self,
            value
    ):
        self._takeout_id = value
        await self._update_session_table()

    async def _update_session_table(self):
        c = await self._cursor()
        # While we can save multiple rows into the sessions table
        # currently we only want to keep ONE as the tables don't
        # tell us which auth_key's are usable and will work. Needs
        # some more work before being able to save auth_key's for
        # multiple DCs. Probably done differently.
        await c.execute('delete from sessions')
        await c.execute('insert or replace into sessions values (?,?,?,?,?)', (
            self._dc_id,
            self._server_address,
            self._port,
            self._auth_key.key if self._auth_key else b'',
            self._takeout_id
        ))
        await c.close()

    async def get_update_state(self, entity_id):
        row = await self._execute('select pts, qts, date, seq from update_state '
                                  'where id = ?', entity_id)
        if row:
            pts, qts, date, seq = row
            date = datetime.datetime.fromtimestamp(
                date, tz=datetime.timezone.utc)
            return types.updates.State(pts, qts, date, seq, unread_count=0)

    async def set_update_state(self, entity_id, state):
        await self._execute('insert or replace into update_state values (?,?,?,?,?)',
                            entity_id, state.pts, state.qts,
                            state.date.timestamp(), state.seq)

    async def get_update_states(self):
        c = await self._cursor()
        try:
            rows = await (
                await c.execute('select id, pts, qts, date, seq from update_state')
            ).fetchall()
            return ((row[0], types.updates.State(
                pts=row[1],
                qts=row[2],
                date=datetime.datetime.fromtimestamp(row[3], tz=datetime.timezone.utc),
                seq=row[4],
                unread_count=0)
                     ) for row in rows)
        finally:
            await c.close()

    async def save(self):
        """Saves the current session object as session_user_id.session"""
        # This is a no-op if there are no changes to commit, so there's
        # no need for us to keep track of an "unsaved changes" variable.
        if self._conn is not None:
            await self._conn.commit()

    async def _cursor(self):
        """Asserts that the connection is open and returns a cursor"""
        if self._conn is None:
            self._conn = await aiosqlite.connect(
                self.filename
            )
        return await self._conn.cursor()

    async def _execute(self, stmt, *values):
        """
        Gets a cursor, executes `stmt` and closes the cursor,
        fetching one row afterwards and returning its result.
        """
        c = await self._cursor()
        try:
            return await (
                await c.execute(stmt, values)
            ).fetchone()
        finally:
            await c.close()

    async def close(self):
        """Closes the connection unless we're working in-memory"""
        if self.filename != ':memory:':
            if self._conn is not None:
                await self._conn.commit()
                await self._conn.close()
                self._conn = None

    async def delete(self):
        """Deletes the current session file"""
        if self.filename == ':memory:':
            return True
        try:
            await aios.remove(self.filename)
            return True
        except OSError:
            return False

    @classmethod
    async def list_sessions(cls):
        """Lists all the sessions of the users who have ever connected
           using this client and never logged out
        """
        return [os.path.splitext(os.path.basename(f))[0]
                for f in os.listdir('.') if f.endswith(EXTENSION)]

    # Entity processing

    async def process_entities(self, tlo):
        """
        Processes all the found entities on the given TLObject,
        unless .save_entities is False.
        """
        if not self.save_entities:
            return

        rows = self._entities_to_rows(tlo)
        if not rows:
            return

        c = await self._cursor()
        try:
            now_tup = (int(time.time()),)
            rows = [row + now_tup for row in rows]
            await c.executemany(
                'insert or replace into entities values (?,?,?,?,?,?)', rows)
        finally:
            await c.close()

    async def get_entity_rows_by_phone(self, phone):
        return await self._execute(
            'select id, hash from entities where phone = ?', phone)

    async def get_entity_rows_by_username(self, username):
        c = await self._cursor()
        try:
            results = await (await c.execute(
                'select id, hash, date from entities where username = ?',
                (username,)
            )).fetchall()

            if not results:
                return None

            # If there is more than one result for the same username, evict the oldest one
            if len(results) > 1:
                results.sort(key=lambda t: t[2] or 0)
                await c.executemany('update entities set username = null where id = ?',
                                    [(t[0],) for t in results[:-1]])

            return results[-1][0], results[-1][1]
        finally:
            await c.close()

    async def get_entity_rows_by_name(self, name):
        return await self._execute(
            'select id, hash from entities where name = ?', name)

    async def get_entity_rows_by_id(self, id, exact=True):
        if exact:
            return await self._execute(
                'select id, hash from entities where id = ?', id)
        else:
            return await self._execute(
                'select id, hash from entities where id in (?,?,?)',
                utils.get_peer_id(PeerUser(id)),
                utils.get_peer_id(PeerChat(id)),
                utils.get_peer_id(PeerChannel(id))
            )

    # File processing

    async def get_file(self, md5_digest, file_size, cls):
        row = await self._execute(
            'select id, hash from sent_files '
            'where md5_digest = ? and file_size = ? and type = ?',
            md5_digest, file_size, _SentFileType.from_type(cls).value
        )
        if row:
            # Both allowed classes have (id, access_hash) as parameters
            return cls(row[0], row[1])

    async def cache_file(self, md5_digest, file_size, instance):
        if not isinstance(instance, (InputDocument, InputPhoto)):
            raise TypeError('Cannot cache %s instance' % type(instance))

        await self._execute(
            'insert or replace into sent_files values (?,?,?,?,?)',
            md5_digest, file_size,
            _SentFileType.from_type(type(instance)).value,
            instance.id, instance.access_hash
        )
