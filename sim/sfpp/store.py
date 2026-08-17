"""A cut-down SF++ store: the firmware's sqlite schema, driven from Python.

The DDL below is copied verbatim from StoreForwardPlusPlusModule's constructor so the simulator
writes the database the firmware reads. Only the parts the set-reconciliation work touches are
modelled - channel_messages, its counter, and the two set-index columns. DMs, canon_scratch and the
peers table exist in the schema but the simulator does not drive them.

Not modelled: the git-chain link walk, whose cost is characterised analytically in chain.py.
"""

import sqlite3

from .sketchindex import (
    BUCKET_OBJECTS,
    BucketSummary,
    bucket_range,
    checksum_contribution,
    short_id,
)

# Verbatim from src/modules/Native/StoreForwardPlusPlus.cpp. Kept as one string per table so a
# diff against the firmware is a straight comparison.
SCHEMA = [
    """CREATE TABLE IF NOT EXISTS channel_messages(
        destination INT NOT NULL,
        sender INT NOT NULL,
        packet_id INT NOT NULL,
        rx_time INT NOT NULL,
        root_hash BLOB NOT NULL,
        encrypted_bytes BLOB NOT NULL,
        message_hash BLOB NOT NULL,
        commit_hash BLOB NOT NULL,
        payload TEXT,
        counter INT DEFAULT 0,
        PRIMARY KEY (message_hash));""",
    """CREATE TABLE IF NOT EXISTS local_messages(
        destination INT NOT NULL,
        sender INT NOT NULL,
        packet_id INT NOT NULL,
        rx_time INT NOT NULL,
        root_hash BLOB NOT NULL,
        encrypted_bytes BLOB NOT NULL,
        message_hash BLOB NOT NULL,
        payload TEXT,
        PRIMARY KEY (message_hash));""",
    """CREATE TABLE IF NOT EXISTS mappings(
        chain_type INT NOT NULL,
        identifier INT NOT NULL,
        root_hash BLOB NOT NULL,
        count INT DEFAULT 0,
        PRIMARY KEY (identifier));""",
    """CREATE TABLE IF NOT EXISTS peers(
        nodenum INT NOT NULL,
        announce_count INT DEFAULT 0,
        query_count INT DEFAULT 0,
        request_count INT DEFAULT 0,
        provide_count INT DEFAULT 0,
        split_count INT DEFAULT 0,
        total_count INT DEFAULT 0,
        average_hops REAL DEFAULT 0,
        PRIMARY KEY (nodenum));""",
]

# The set-reconciliation columns arrived after the table, so an existing store gets them here.
MIGRATIONS = [
    "ALTER TABLE channel_messages ADD COLUMN short_id INT;",
    "ALTER TABLE channel_messages ADD COLUMN checksum INT;",
]

# The bucket summary query, verbatim - the simulator must summarise a bucket the way the firmware
# does, including the `short_id is not null` clause that skips un-backfilled rows.
BUCKET_SUMMARY_SQL = """select short_id, checksum from channel_messages
    where substr(root_hash,1,?)=? and counter>=? and counter<=? and short_id is not null;"""


class SfppStore:
    """One node's store, sharing nothing with any other."""

    def __init__(self, path, node_id, index_on_insert=True):
        self.node_id = node_id
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL;")
        for statement in SCHEMA:
            self.db.execute(statement)
        for statement in MIGRATIONS:
            try:
                self.db.execute(statement)
            except sqlite3.OperationalError:
                pass  # duplicate column - the expected result on every run after the first
        self.db.commit()
        # Whether the set index is written at ingest. False reproduces a store that predates the
        # columns, so backfill() has something to do.
        self.index_on_insert = index_on_insert

    # --- ingest ---

    def insert(self, message, counter):
        """Store one object at its chain counter. Returns False if it was already held.

        The counter is assigned by whoever owns the chain, not by the receiver, because buckets are
        counter-based: two nodes only summarise the same bucket if they agree on the numbering.
        """
        held = self.db.execute(
            "select 1 from channel_messages where message_hash=?;",
            (message.message_hash,),
        ).fetchone()
        if held:
            return False

        sid = short_id(message.message_hash) if self.index_on_insert else None
        ck = (
            checksum_contribution(message.message_hash)
            if self.index_on_insert
            else None
        )
        self.db.execute(
            """insert into channel_messages
               (destination, sender, packet_id, rx_time, root_hash, encrypted_bytes,
                message_hash, commit_hash, payload, counter, short_id, checksum)
               values (?,?,?,?,?,?,?,?,?,?,?,?);""",
            (
                message.destination,
                message.sender,
                message.packet_id,
                message.rx_time,
                message.root_hash,
                message.encrypted_bytes,
                message.message_hash,
                message.commit_hash,
                message.payload,
                counter,
                sid,
                # sqlite INTEGER is signed 64-bit; the checksum is not. Round-trip through the
                # same two's-complement reading the C++ int64 bind/column pair performs.
                None if ck is None else as_signed64(ck),
            ),
        )
        self.db.commit()
        return True

    def backfill(self, limit=128):
        """Mirror of backfillSetIndex(): derive both identifiers for rows that predate the columns."""
        rows = self.db.execute(
            "select message_hash from channel_messages where short_id is null limit ?;",
            (limit,),
        ).fetchall()
        for (message_hash,) in rows:
            self.db.execute(
                "update channel_messages set short_id=?, checksum=? where message_hash=?;",
                (
                    short_id(message_hash),
                    as_signed64(checksum_contribution(message_hash)),
                    message_hash,
                ),
            )
        self.db.commit()
        return len(rows)

    # --- summarising ---

    def build_bucket_summary(
        self, root_hash, bucket, capacity=BUCKET_OBJECTS, root_prefix_len=None
    ):
        """Mirror of buildBucketSummary(). Returns None when the bucket holds nothing."""
        prefix_len = len(root_hash) if root_prefix_len is None else root_prefix_len
        first, last = bucket_range(bucket)
        summary = BucketSummary(capacity)
        for sid, ck in self.db.execute(
            BUCKET_SUMMARY_SQL, (prefix_len, root_hash[:prefix_len], first, last)
        ):
            summary.add(sid, as_unsigned64(ck))
        return summary if summary.count > 0 else None

    # --- queries the simulator needs that the firmware gets from its own state ---

    def holds(self, message_hash):
        return (
            self.db.execute(
                "select 1 from channel_messages where message_hash=?;", (message_hash,)
            ).fetchone()
            is not None
        )

    def hash_for_short_id(self, sid):
        """Resolve a decoded sketch member back to the object. None if this node does not hold it.

        A short ID is truncated, so this can match more than one row; the caller is expected to
        confirm against the checksum rather than trust the first hit.
        """
        rows = self.db.execute(
            "select message_hash from channel_messages where short_id=?;", (sid,)
        ).fetchall()
        return [r[0] for r in rows]

    def tip_counter(self, root_hash):
        row = self.db.execute(
            "select max(counter) from channel_messages where root_hash=?;", (root_hash,)
        ).fetchone()
        return row[0] or 0

    def count(self, root_hash=None):
        if root_hash is None:
            return self.db.execute("select count(*) from channel_messages;").fetchone()[
                0
            ]
        return self.db.execute(
            "select count(*) from channel_messages where root_hash=?;", (root_hash,)
        ).fetchone()[0]

    def close(self):
        self.db.close()


def as_signed64(value):
    """Reinterpret an unsigned 64-bit value the way sqlite3_bind_int64 receives it."""
    return value - (1 << 64) if value >= (1 << 63) else value


def as_unsigned64(value):
    """The inverse, for reading a column back into a checksum."""
    return value + (1 << 64) if value < 0 else value
