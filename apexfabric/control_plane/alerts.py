"""Numeric event thresholds; evaluated in the same transaction as event ingestion."""
import json
import math
import operator
import re
import time
import uuid

OPS = {'gt': operator.gt, 'gte': operator.ge, 'lt': operator.lt, 'lte': operator.le, 'eq': operator.eq}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS alert_rules (
 id TEXT PRIMARY KEY, definition TEXT NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
 id TEXT PRIMARY KEY, rule_id TEXT NOT NULL, event_id TEXT NOT NULL,
 created_at REAL NOT NULL, acknowledged_at REAL, evidence TEXT NOT NULL,
 UNIQUE(rule_id,event_id)
);
CREATE INDEX IF NOT EXISTS alerts_created ON alerts(created_at);
CREATE TABLE IF NOT EXISTS alert_rule_state (
 rule_id TEXT NOT NULL REFERENCES alert_rules(id) ON DELETE CASCADE,
 deployment_id TEXT NOT NULL, camera_id TEXT NOT NULL,
 active INTEGER NOT NULL, last_trigger REAL NOT NULL,
 PRIMARY KEY(rule_id,deployment_id,camera_id)
);
'''


def numeric(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def field_value(payload, path):
    value = payload
    for part in path.split('.'):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def validate_rule(rule):
    allowed = {'id', 'name', 'event_type', 'field', 'operator', 'threshold', 'camera_id', 'deployment_id', 'cooldown_seconds', 'enabled'}
    if not isinstance(rule, dict) or set(rule) - allowed:
        raise ValueError('invalid alert rule fields')
    result = dict(rule)
    for key in ('name', 'event_type', 'field'):
        if not isinstance(result.get(key), str) or not 1 <= len(result[key]) <= 160:
            raise ValueError(f'{key} is required (maximum 160 characters)')
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){0,7}', result['field']):
        raise ValueError('field must be a dotted object field path')
    if result.get('operator') not in OPS or not numeric(result.get('threshold')):
        raise ValueError('a supported comparison and finite numeric threshold are required')
    for key in ('camera_id', 'deployment_id'):
        result.setdefault(key, '')
        if not isinstance(result[key], str) or len(result[key]) > 160:
            raise ValueError(f'{key} is invalid')
    result.setdefault('enabled', True)
    result.setdefault('cooldown_seconds', 300)
    if type(result['enabled']) is not bool:
        raise ValueError('enabled must be boolean')
    cooldown = result['cooldown_seconds']
    if type(cooldown) is not int or not 0 <= cooldown <= 86400:
        raise ValueError('cooldown_seconds must be an integer between 0 and 86400')
    if 'id' in result and (not isinstance(result['id'], str) or not re.fullmatch(r'[a-f0-9]{32}', result['id'])):
        raise ValueError('invalid rule ID')
    return result


def evaluate(connection, event_id, deployment_id, payload, received_at):
    """Only new, accepted events reach this function; no historical backfill."""
    event_type = payload.get('type') or payload.get('event_type') or payload.get('event')
    camera = payload.get('camera_id') or field_value(payload, 'data.camera_id') or field_value(payload, 'camera.camera_id') or ''
    if not isinstance(camera, str):
        camera = ''
    for row in connection.execute('SELECT definition FROM alert_rules'):
        rule = json.loads(row[0])
        if not rule['enabled'] or event_type != rule['event_type']:
            continue
        if rule['deployment_id'] and rule['deployment_id'] != deployment_id:
            continue
        if rule['camera_id'] and rule['camera_id'] != camera:
            continue
        value = field_value(payload, rule['field'])
        if not numeric(value):
            continue
        state = connection.execute('SELECT active,last_trigger FROM alert_rule_state WHERE rule_id=? AND deployment_id=? AND camera_id=?', (rule['id'], deployment_id, camera)).fetchone()
        active, last = (state[0], state[1]) if state else (0, 0)
        matched = OPS[rule['operator']](value, rule['threshold'])
        # Require a non-matching numeric event to rearm. A suppressed crossing
        # remains active, preventing a late popup without another crossing.
        if matched and not active and (not state or last == 0 or received_at - last >= rule['cooldown_seconds']):
            evidence = {'rule': rule, 'deployment_id': deployment_id, 'camera_id': camera,
                        'event_type': event_type, 'value': value, 'occurred_at': payload.get('time') or payload.get('occurred_at') or payload.get('timestamp')}
            connection.execute('INSERT OR IGNORE INTO alerts VALUES (?,?,?,?,NULL,?)',
                               (uuid.uuid4().hex, rule['id'], event_id, received_at, json.dumps(evidence)))
            last = received_at
        connection.execute('INSERT OR REPLACE INTO alert_rule_state VALUES (?,?,?,?,?)',
                           (rule['id'], deployment_id, camera, int(matched), last))
    prune(connection, received_at)


def prune(connection, now):
    # Alerts retain their own small evidence independently of event snapshots.
    connection.execute('DELETE FROM alerts WHERE created_at < ?', (now - 30 * 86400,))
    connection.execute('DELETE FROM alerts WHERE id IN (SELECT id FROM alerts ORDER BY created_at DESC,id DESC LIMIT -1 OFFSET 10000)')


class AlertStore:
    def __init__(self, telemetry):
        self.telemetry = telemetry

    def rules(self):
        with self.telemetry._connect() as connection:
            return [json.loads(r[0]) for r in connection.execute('SELECT definition FROM alert_rules ORDER BY updated_at DESC,id')]

    def save(self, rule):
        rule = validate_rule(rule)
        with self.telemetry.lock, self.telemetry._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            if 'id' in rule:
                if not connection.execute('SELECT 1 FROM alert_rules WHERE id=?', (rule['id'],)).fetchone():
                    raise ValueError('unknown rule ID')
            else:
                if connection.execute('SELECT COUNT(*) FROM alert_rules').fetchone()[0] >= 100:
                    raise ValueError('maximum 100 alert rules')
                rule['id'] = uuid.uuid4().hex
            connection.execute('INSERT OR REPLACE INTO alert_rules VALUES (?,?,?)', (rule['id'], json.dumps(rule), time.time()))
            connection.execute('DELETE FROM alert_rule_state WHERE rule_id=?', (rule['id'],))
        return rule

    def delete(self, rule_id):
        with self.telemetry.lock, self.telemetry._connect() as connection:
            connection.execute('DELETE FROM alert_rules WHERE id=?', (rule_id,))

    def list(self):
        with self.telemetry._connect() as connection:
            prune(connection, time.time())
            rows = connection.execute('SELECT * FROM alerts ORDER BY (acknowledged_at IS NULL) DESC,created_at DESC,id DESC LIMIT 100').fetchall()
            return [{**dict(r), 'evidence': json.loads(r['evidence'])} for r in rows]

    def acknowledge(self, alert_id):
        with self.telemetry._connect() as connection:
            changed = connection.execute('UPDATE alerts SET acknowledged_at=COALESCE(acknowledged_at,?) WHERE id=?', (time.time(), alert_id)).rowcount
            if not changed:
                raise ValueError('unknown alert ID')
