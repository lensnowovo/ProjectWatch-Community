"""Durable automatic push intent journal; ambiguous delivery is never resent.

Per-member keys prevent an old ambiguous event being resent inside a new batch.
No webhook, message content or credentials enter this table. Acknowledged sends
are reconciled locally after a crash. Explicit vendor rejections get at most
three attempts; missing acknowledgements stay held (not exactly-once delivery).
"""

import hashlib
import uuid
from contextlib import closing

from project_watch.studio import digest
from project_watch.studio.http import HttpError
from project_watch.studio.store import now

SCHEMA = '''CREATE TABLE IF NOT EXISTS studio_notification_deliveries (
 provider TEXT NOT NULL, channel TEXT NOT NULL, member_key TEXT NOT NULL,
 status TEXT NOT NULL, attempts INTEGER NOT NULL, batch TEXT NOT NULL, error_code TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(provider,channel,member_key)
);'''


class NotificationDelivery:
    def __init__(self, store):
        self.store = store
        with closing(store._connect()) as db, db:
            db.executescript(SCHEMA)

    def deliver(self, http, config, channel, members, title, build, *, batch_limit=10):
        if not 1 <= batch_limit <= 10 or len(members) > 500:
            raise ValueError('notification batch exceeds bounds')
        provider = hashlib.sha256((config.kind + ':' + config.webhook_url).encode()).hexdigest()
        # Members are caller-controlled stable local IDs, never raw documents.
        keys = {member: hashlib.sha256(str(member).encode()).hexdigest() for member in members}
        ready, acknowledged, held = [], [], []
        batch = uuid.uuid4().hex
        with closing(self.store._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            for member, key in keys.items():
                old = db.execute('SELECT status,attempts FROM studio_notification_deliveries WHERE provider=? AND channel=? AND member_key=?',
                                 (provider, channel, key)).fetchone()
                if old and old['status'] == 'acknowledged':
                    acknowledged.append(member)
                    continue
                if old and (old['status'] != 'rejected' or old['attempts'] >= 3):
                    held.append(member)
                    continue
                if len(ready) >= batch_limit:
                    continue  # Leave the rest unclaimed for the next bounded delivery.
                ready.append(member)
                db.execute("INSERT INTO studio_notification_deliveries VALUES (?,?,?,'started',1,?,'',?,?) "
                           "ON CONFLICT(provider,channel,member_key) DO UPDATE SET status='started',attempts=attempts+1,batch=excluded.batch,error_code='',updated_at=excluded.updated_at",
                           (provider, channel, key, batch, now(), now()))
        if ready:
            try:
                digest.send(http, config, title, build(ready))
            except Exception as exc:
                # Only a valid vendor rejection or definite HTTP 4xx permits retry.
                retryable = isinstance(exc, HttpError) and (exc.code == 'notify_rejected' or (exc.code == 'notify_failed' and exc.status in (400, 401, 403, 404, 429)))
                with closing(self.store._connect()) as db, db:
                    db.execute('UPDATE studio_notification_deliveries SET status=?,error_code=?,updated_at=? WHERE provider=? AND channel=? AND batch=?',
                               ('rejected' if retryable else 'unknown', 'notify_rejected' if retryable else 'notify_outcome_unknown', now(), provider, channel, batch))
                if isinstance(exc, HttpError):
                    raise HttpError(exc.code if retryable else 'notify_outcome_unknown', status=exc.status) from None
                raise HttpError('notify_outcome_unknown') from None
            with closing(self.store._connect()) as db, db:
                db.execute("UPDATE studio_notification_deliveries SET status='acknowledged',error_code='',updated_at=? WHERE provider=? AND channel=? AND batch=? AND status='started'",
                           (now(), provider, channel, batch))
            acknowledged.extend(ready)
        return {'acknowledged': acknowledged, 'sent': len(ready), 'held': len(held)}
