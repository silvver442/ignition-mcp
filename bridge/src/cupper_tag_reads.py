# Jython fragment appended to the Bridge 0.1.3 library by apply_upgrade_014.py.

def _cupper_migration_request(payload):
    name = _cupper_tag_name(payload.get('path'))
    mode = payload.get('mode')
    if mode not in ('prepare', 'restore'):
        raise ValueError('Expected Cupper operation prepare or restore')
    if payload.get('exclusiveAccess') is not True:
        raise ValueError('Explicit exclusiveAccess boolean true is required')
    return name, mode


def handle_tags_scoped_cupper_validate(payload):
    name, mode = _cupper_migration_request(payload)
    path = payload['path']
    _scoped_tag_path(path)
    return {'valid': True, 'path': path, 'name': name, 'mode': mode, 'exclusiveAccess': True}


def _cupper_raw_history(path, start_ms, end_ms):
    if (not _cupper_integer(start_ms) or not _cupper_integer(end_ms)
            or not 0 <= start_ms < end_ms <= system.date.now().getTime()
            or end_ms - start_ms > 7 * 86400000):
        raise ValueError('Expected explicit start_ms and end_ms for a closed window of at most seven days')
    history_path = ('histprov:Postgres_Historian:/drv:Ignition-Prox-Maker:default:'
                    '/tag:Line_1/Cupper/Ignition/Production')
    dataset = system.historian.queryRawPoints(
        paths=[history_path], startTime=system.date.fromMillis(start_ms),
        endTime=system.date.fromMillis(end_ms), returnFormat='TALL', returnSize=-1,
    )
    if dataset.getColumnCount() != 4:
        raise ValueError('Expected path, value, quality and timestamp in the TALL historian dataset')
    rows = []
    for index in range(dataset.getRowCount()):
        timestamp_ms = dataset.getValueAt(index, 3).getTime()
        if start_ms <= timestamp_ms <= end_ms:
            value = dataset.getValueAt(index, 1)
            if value is not None and not _cupper_integer(value):
                raise ValueError('Expected integer samples from SQL Historian')
            quality = dataset.getValueAt(index, 2)
            sample = {'timestamp_ms': timestamp_ms, 'value': value, 'path': path,
                      'quality': None if quality is None else unicode(quality)}
            encoded = json.dumps(sample, sort_keys=True, ensure_ascii=True, separators=(',', ':'))
            rows.append((timestamp_ms, encoded, sample))
    rows.sort(key=lambda row: row[:2])
    digest = hashlib.sha256('\n'.join(row[1] for row in rows).encode('ascii')).hexdigest()
    return {'path': path, 'start_ms': start_ms, 'end_ms': end_ms, 'count': len(rows),
            'sha256': digest, 'samples': [row[2] for row in rows]}
