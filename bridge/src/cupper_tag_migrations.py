# Jython functions for the existing scoped Cupper migration and history routes.

def _cupper_tag_name(path):
    root = '[default]Line_1/Cupper/Ignition/'
    if path not in (root + 'Speed', root + 'Production'):
        raise ValueError('Migration is limited to Cupper Speed and Production')
    return path.rsplit('/', 1)[1]


def _cupper_integer(value):
    return isinstance(value, (int, long)) and not isinstance(value, bool)


def _cupper_history_proof(start=None, end=None, name='Production'):
    if name not in ('Production', 'Speed'):
        raise ValueError('History proof is limited to Cupper Production and Speed')
    window = 1800000 if name == 'Speed' else 7 * 86400000
    cutoff = system.date.now().getTime() - 300000
    if start is None and end is None:
        end = cutoff
        start = end - window
    if (not _cupper_integer(start) or not _cupper_integer(end)
            or not 0 <= start < end <= cutoff or end - start > window):
        limit = '30 minutes' if name == 'Speed' else 'seven days'
        raise ValueError('Expected a closed historical window of at most ' + limit)
    path = 'histprov:Postgres_Historian:/drv:Ignition-Prox-Maker:default:/tag:Line_1/Cupper/Ignition/' + name
    options = {'returnSize': -1} if name == 'Speed' else {}
    dataset = system.historian.queryRawPoints(paths=[path], startTime=system.date.fromMillis(start),
                                             endTime=system.date.fromMillis(end), returnFormat='TALL', **options)
    if dataset.getColumnCount() != 4:
        raise ValueError('Expected a timestamp, value and quality from SQL Historian')
    rows = []
    numeric_samples = 0
    for index in range(dataset.getRowCount()):
        timestamp = dataset.getValueAt(index, 3).getTime()
        if start <= timestamp < end:
            value = dataset.getValueAt(index, 1)
            if value is not None and not _cupper_integer(value):
                raise ValueError('Expected integer samples from SQL Historian')
            if value is not None:
                numeric_samples += 1
            quality = dataset.getValueAt(index, 2)
            rows.append(json.dumps([timestamp, value, str(quality)], separators=(',', ':')))
    if rows and not numeric_samples:
        raise ValueError('SQL Historian returned no numeric %s samples for the window' % name)
    digest = hashlib.sha256('\n'.join(sorted(rows)).encode('utf-8')).hexdigest()
    return {'start': start, 'end': end, 'count': len(rows), 'sha256': digest}


def handle_tags_scoped_cupper_history(payload):
    path = payload.get('path')
    name = _cupper_tag_name(path)
    if name == 'Production' and set(payload) == {'path', 'start_ms', 'end_ms'}:
        return _cupper_raw_history(path, payload['start_ms'], payload['end_ms'])
    if set(payload) not in ({'path'}, {'path', 'start', 'end'}) or (
            'start' in payload and (payload['start'] is None or payload['end'] is None)):
        raise ValueError('Expected both legacy history boundaries')
    return _cupper_history_proof(payload.get('start'), payload.get('end'), name)


def _cupper_same_config(first, second, fields):
    return (dict((k, v) for k, v in first.items() if k not in fields)
            == dict((k, v) for k, v in second.items() if k not in fields))


def _cupper_require_snapshot(snapshot, data_type, source):
    effective = snapshot['effective']
    if (snapshot['config'] is None or effective.get('dataType') != data_type
            or effective.get('valueSource') != source or effective.get('historyEnabled') is not True
            or effective.get('historyProvider') != 'Postgres_Historian'
            or not snapshot['valueGood'] or not _cupper_integer(snapshot['value'])):
        raise ValueError('Unexpected Cupper type, source, history or live value')


def handle_tags_scoped_cupper_migrate(payload):
    name, mode = _cupper_migration_request(payload)
    path = payload.get('path')
    restoring = mode == 'restore'
    with TAG_CONFIG_LOCK:
        parent, _ = _scoped_tag_path(path)
        before = _scoped_tag_snapshot(path, parent, name)
        config = before['config']
        if config != payload.get('expectedConfig'):
            raise ValueError('Tag configuration changed before migration')
        value = before['value']
        if name == 'Production':
            _cupper_require_snapshot(before, 'Int8' if restoring else 'Int4', 'memory')
            proof = payload.get('historyProof')
            if (not isinstance(proof, dict) or not proof.get('count')
                    or _cupper_history_proof(proof.get('start'), proof.get('end')) != proof):
                raise ValueError('Production raw history no longer matches its preflight')
            if value != payload.get('expectedValue') or not -(2 ** 31) <= value < 2 ** 31:
                raise ValueError('Production counter changed or is outside the Int4 range')
            # Read again after the historian query and as close to configure as possible.
            latest = _scoped_tag_snapshot(path, parent, name)
            if latest != before:
                raise ValueError('Production changed during historian verification')
            patch = {'name': name, 'dataType': 'Int4' if restoring else 'Int8', 'value': value}
            _scoped_result(system.tag.configure(parent, [patch], 'm'))
        elif restoring:
            _cupper_require_snapshot(before, 'Int4', 'memory')
            prior = payload.get('beforeConfig')
            if (value != payload.get('expectedValue') or not isinstance(prior, dict)
                    or prior.get('valueSource') != 'expr' or prior.get('dataType', 'Int4') != 'Int4'
                    or not isinstance(prior.get('expression'), basestring) or not prior['expression']
                    or not _cupper_same_config(config, prior, ('valueSource', 'expression', 'value', 'defaultValue'))):
                raise ValueError('Speed has advanced or its original expression cannot be restored safely')
            _scoped_result(system.tag.configure(parent, [prior], 'o'))
        else:
            _cupper_require_snapshot(before, 'Int4', 'expr')
            proof = payload.get('historyProof')
            if (not isinstance(proof, dict) or not proof.get('count')
                    or _cupper_history_proof(proof.get('start'), proof.get('end'), name) != proof):
                raise ValueError('Speed raw history no longer matches its preflight')
            latest = _scoped_tag_snapshot(path, parent, name)
            _cupper_require_snapshot(latest, 'Int4', 'expr')
            effective = latest['effective']
            if (latest['config'] != config or effective != before['effective']
                    or effective.get('sampleMode') != 'Periodic' or effective.get('historySampleRate') != 10
                    or effective.get('historySampleRateUnits') != 'SEC'):
                raise ValueError('Speed configuration or 10-second history settings changed before migration')
            value = latest['value']
            patch = {'name': name, 'valueSource': 'memory', 'defaultValue': value, 'value': value}
            _scoped_result(system.tag.configure(parent, [patch], 'm'))
        return {'valueBefore': value}
