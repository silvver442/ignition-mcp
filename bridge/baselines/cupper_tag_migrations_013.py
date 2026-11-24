# Jython fragment inserted into the Bridge library by apply_upgrade_013.py.

def _cupper_tag_name(path):
    root = '[default]Line_1/Cupper/Ignition/'
    if path not in (root + 'Speed', root + 'Production'):
        raise ValueError('Migration is limited to Cupper Speed and Production')
    return path.rsplit('/', 1)[1]


def _cupper_integer(value):
    return isinstance(value, (int, long)) and not isinstance(value, bool)


def _cupper_history_proof(start=None, end=None):
    day = 86400000
    cutoff = system.date.now().getTime() - 300000
    if start is None and end is None:
        end = cutoff
        start = end - 7 * day
    if (not _cupper_integer(start) or not _cupper_integer(end)
            or not 0 <= start < end <= cutoff or end - start > 7 * day):
        raise ValueError('Expected a closed historical window of at most seven days')
    path = 'histprov:Postgres_Historian:/drv:Ignition-Prox-Maker:default:/tag:Line_1/Cupper/Ignition/Production'
    dataset = system.historian.queryRawPoints(paths=[path], startTime=system.date.fromMillis(start),
                                             endTime=system.date.fromMillis(end), returnFormat='TALL')
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
        raise ValueError('SQL Historian returned no numeric Production samples for the window')
    digest = hashlib.sha256('\n'.join(sorted(rows)).encode('utf-8')).hexdigest()
    return {'start': start, 'end': end, 'count': len(rows), 'sha256': digest}


def handle_tags_scoped_cupper_history(payload):
    if payload.get('path') != '[default]Line_1/Cupper/Ignition/Production':
        raise ValueError('Only Cupper Production historical samples are supported')
    return _cupper_history_proof(payload.get('start'), payload.get('end'))


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
    path = payload.get('path')
    name = _cupper_tag_name(path)
    mode = payload.get('mode')
    if mode not in ('prepare', 'restore') or payload.get('exclusiveAccess') is not True:
        raise ValueError('An exact operation and exclusive access to the tag are required')
    restoring = mode == 'restore'
    with TAG_CONFIG_LOCK:
        parent, _ = _scoped_tag_path(path)
        before = _scoped_tag_snapshot(path, parent, name)
        config = before['config']
        if config != payload.get('expectedConfig'):
            raise ValueError('Tag configuration changed before migration')
        value = before['value']
        policy = 'm'
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
        elif restoring:
            _cupper_require_snapshot(before, 'Int4', 'memory')
            prior = payload.get('beforeConfig')
            if (value != payload.get('expectedValue') or not isinstance(prior, dict)
                    or prior.get('valueSource') != 'expr' or prior.get('dataType', 'Int4') != 'Int4'
                    or not isinstance(prior.get('expression'), basestring) or not prior['expression']
                    or not _cupper_same_config(config, prior, ('valueSource', 'expression', 'value', 'defaultValue'))):
                raise ValueError('Speed has advanced or its original expression cannot be restored safely')
            patch = prior
            policy = 'o'
        else:
            _cupper_require_snapshot(before, 'Int4', 'expr')
            patch = {'name': name, 'valueSource': 'memory', 'defaultValue': value, 'value': value}
        _scoped_result(system.tag.configure(parent, json.dumps([patch]), policy))
        return {'valueBefore': value}
